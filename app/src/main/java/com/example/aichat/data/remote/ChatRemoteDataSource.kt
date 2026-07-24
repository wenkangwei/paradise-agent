package com.example.aichat.data.remote

import com.example.aichat.data.local.entity.ApiProfileEntity
import com.example.aichat.data.provider.LlmProvider
import com.example.aichat.data.provider.LlmProviderFactory
import com.example.aichat.data.provider.StreamEvent
import com.example.aichat.data.repository.ApiProfileRepository
import com.example.aichat.data.security.ApiKeyEncryptor
import com.example.aichat.domain.model.Message
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Thrown when the AI API returns a non-2xx response.
 */
class ApiException(message: String, cause: Throwable? = null) : Exception(message, cause)

/**
 * Thrown when a network error occurs during the API call.
 */
class NetworkException(message: String, cause: Throwable? = null) : Exception(message, cause)

/**
 * Lightweight attachment descriptor for building multimodal message DTOs.
 *
 * [url] can be either an http(s) URL or a base64 data URL produced by [AttachmentEncoder].
 */
data class Attachment(
    val url: String,
    val mimeType: String = "image/jpeg"
)

/**
 * Remote data source for AI chat streaming.
 *
 * Bridges the domain [Message] type to [LlmProvider.stream] — it no longer owns
 * a Retrofit singleton or reads baseUrl/apiKey from [ConfigManager]. Instead, the
 * active [ApiProfileEntity] is resolved at call time via [ApiProfileRepository],
 * and the concrete OkHttp/Retrofit stack is supplied (and cached) by
 * [LlmProviderFactory] so that switching profiles takes effect immediately
 * without restarting the app (this was the original Singleton Retrofit bug).
 *
 * Emits [StreamEvent]s — callers discriminate between content, reasoning, and
 * finish events instead of consuming raw token strings.
 */
@Singleton
class ChatRemoteDataSource @Inject constructor(
    private val providerFactory: LlmProviderFactory,
    private val apiProfileRepo: ApiProfileRepository,
    private val apiKeyEncryptor: ApiKeyEncryptor
) {

    /**
     * Streams a chat completion, emitting structured [StreamEvent]s.
     *
     * @param messages conversation history (text-only)
     * @param model optional model override; falls back to the active profile's model
     */
    fun streamChat(
        messages: List<Message>,
        model: String? = null
    ): Flow<StreamEvent> = flow {
        val provider = resolveProvider()
        emitAll(provider.stream(messages, attachments = null, model = model))
    }

    /**
     * Streams a multimodal chat completion (text + image attachments).
     *
     * @param messages list of (domain Message, attachments) pairs
     * @param model optional model override
     */
    fun streamChatMultimodal(
        messages: List<Pair<Message, List<Attachment>>>,
        model: String? = null
    ): Flow<StreamEvent> = flow {
        val provider = resolveProvider()
        val domainMessages = messages.map { it.first }
        val remoteAttachments = messages.map { it.second }
        emitAll(provider.stream(domainMessages, remoteAttachments, model))
    }

    /**
     * Resolves the active profile and constructs (or fetches a cached)
     * [LlmProvider] for it. Throws [IllegalStateException] if no profile is
     * configured — callers should have been seeded by [ApiProfileBootstrap].
     */
    private suspend fun resolveProvider(): LlmProvider {
        val profile = apiProfileRepo.getActive()
            ?: error("No active ApiProfile; ApiProfileBootstrap.ensureSeeded() was not called.")
        // Decrypt the plaintext key once per stream and bake it into the
        // ApiProfileEntity snapshot we hand to the factory. Subsequent calls
        // hit the LlmProviderFactory LRU cache, keyed by id — so a key rotation
        // invalidates only on next miss, not on every request.
        val plainKey = apiKeyEncryptor.decrypt(profile.id).orEmpty()
        val entity = ApiProfileEntity(
            id = profile.id,
            title = profile.title,
            supplierId = profile.supplierId,
            baseUrl = profile.baseUrl,
            apiKeyEncrypted = plainKey,
            modelName = profile.modelName,
            isDefault = true,
            customFieldsJson = "{}",
            createdAt = profile.createdAt,
            updatedAt = profile.updatedAt
        )
        return providerFactory.get(entity)
    }

    /** Delegates emission to the upstream flow without re-collecting manually. */
    private suspend fun <T> kotlinx.coroutines.flow.FlowCollector<T>.emitAll(
        source: Flow<T>
    ) {
        source.collect { emit(it) }
    }
}
