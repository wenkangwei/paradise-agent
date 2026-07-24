package com.example.aichat.data.provider

import com.example.aichat.data.local.entity.ApiProfileEntity
import com.example.aichat.data.remote.AiApiService
import com.example.aichat.data.remote.AiStreamClient
import com.example.aichat.data.remote.Attachment as RemoteAttachment
import com.example.aichat.data.remote.ChatRemoteDataSource
import com.example.aichat.data.remote.dto.ChatRequestDto
import com.example.aichat.data.remote.dto.ContentPart
import com.example.aichat.data.remote.dto.ImageUrlData
import com.example.aichat.data.remote.dto.MessageDto
import com.example.aichat.domain.model.Message
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn
import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.HttpException
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.io.IOException
import java.util.LinkedHashMap
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Creates (and caches) [LlmProvider] instances per [ApiProfileEntity].
 *
 * Why a factory instead of @Singleton Retrofit:
 *   The original code bound Retrofit as @Singleton with a single baseUrl read at
 *   startup. Changing the active API profile therefore required restarting the app.
 *   This factory builds a fresh OkHttp+Retrofit+AiApiService per profile and caches
 *   up to [MAX_CACHE_SIZE] of them, so switching profiles is instant.
 *
 * CONTRACT: [ApiProfileEntity.apiKeyEncrypted] is treated as PLAINTEXT by the
 * factory. Callers (ChatRemoteDataSource) MUST decrypt the stored ciphertext
 * via [com.example.aichat.data.security.ApiKeyEncryptor.decrypt] before passing
 * the entity in. The column name is preserved for migration continuity; the
 * plaintext never lands in the database.
 */
interface LlmProviderFactory {
    fun get(profile: ApiProfileEntity): LlmProvider
}

@Singleton
class LlmProviderFactoryImpl @Inject constructor(
    private val supplierRegistry: SupplierRegistry,
    private val streamClient: AiStreamClient
) : LlmProviderFactory {

    private val cache = object : LinkedHashMap<String, LlmProvider>(CACHE_INITIAL, 0.75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, LlmProvider>?): Boolean {
            return size > MAX_CACHE_SIZE
        }
    }

    @Synchronized
    override fun get(profile: ApiProfileEntity): LlmProvider {
        return cache.getOrPut(profile.id) {
            val supplier = supplierRegistry.byId(profile.supplierId) ?: CustomSupplier
            OpenAiCompatibleProvider(profile, supplier, streamClient)
        }
    }

    companion object {
        private const val MAX_CACHE_SIZE = 4
        private const val CACHE_INITIAL = 8
    }
}

/**
 * Handles every supplier whose protocol is OpenAI-compatible.
 *
 * Builds a dedicated OkHttp+Retrofit stack per profile so that baseUrl/apiKey
 * are baked in at creation time (no interceptor state, no race when switching
 * profiles mid-request).
 */
private class OpenAiCompatibleProvider(
    private val profile: ApiProfileEntity,
    private val supplier: Supplier,
    private val streamClient: AiStreamClient
) : LlmProvider {

    private val apiService: AiApiService = buildStack()

    override fun stream(
        messages: List<Message>,
        attachments: List<List<RemoteAttachment>>?,
        model: String?
    ): Flow<StreamEvent> = flow {
        val request = ChatRequestDto(
            model = model ?: profile.modelName,
            messages = if (attachments != null) {
                messages.mapIndexed { i, m -> m.toDto(attachments.getOrNull(i).orEmpty()) }
            } else {
                messages.map { it.toDto() }
            },
            stream = true
        )

        val responseBody = try {
            apiService.streamChat(request)
        } catch (e: HttpException) {
            throw com.example.aichat.data.remote.ApiException(
                "API error: HTTP ${e.code()} - ${e.message()}", e
            )
        } catch (e: IOException) {
            throw com.example.aichat.data.remote.NetworkException(
                "Network error: ${e.message}", e
            )
        }

        streamClient.toEventFlow(responseBody).collect { emit(it) }
    }.flowOn(Dispatchers.IO)

    private fun buildStack(): AiApiService {
        val baseUrl = profile.baseUrl.let { if (it.endsWith("/")) it else "$it/" }

        // Logging: NONE in streaming path. Even BASIC level reads every response
        // chunk to print its metadata, which forces buffering and delays SSE
        // delivery to the parser → looks like GLM is "slow" when it's actually
        // OkHttp holding back chunks for the logger. Set Level.NONE to be safe.
        val logging = HttpLoggingInterceptor().apply {
            level = HttpLoggingInterceptor.Level.NONE
        }
        val auth = Interceptor { chain ->
            val builder = chain.request().newBuilder()
                .addHeader("Authorization", "Bearer ${profile.apiKeyEncrypted}")
                .addHeader("Content-Type", "application/json")
                .addHeader("Accept", "text/event-stream")
                // Disable gzip on the SSE stream. If the server returns gzip'd
                // chunks OkHttp waits for a full compressed block before
                // decompressing — visible as "bursty" token delivery.
                .addHeader("Accept-Encoding", "identity")
                // Hint to intermediate proxies / CDN to disable buffering too.
                .addHeader("Cache-Control", "no-cache")
                .addHeader("Connection", "keep-alive")
            supplier.extraHeaders.forEach { (k, v) -> builder.addHeader(k, v) }
            chain.proceed(builder.build())
        }

        val client = OkHttpClient.Builder()
            .addInterceptor(auth)
            .addInterceptor(logging)
            .connectionPool(sharedConnectionPool)
            .connectTimeout(30, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.SECONDS)   // no read timeout for streaming
            .writeTimeout(30, TimeUnit.SECONDS)
            .callTimeout(0, TimeUnit.SECONDS)   // no overall cap; stream stays open
            .retryOnConnectionFailure(true)
            .build()

        val retrofit = Retrofit.Builder()
            .baseUrl(baseUrl)
            .client(client)
            .addConverterFactory(GsonConverterFactory.create())
            .build()

        return retrofit.create(AiApiService::class.java)
    }

    companion object {
        // One shared pool across all profiles so DNS/TLS warmups persist when
        // the user switches profiles. Without this, switching profile = new
        // OkHttp = new pool = cold TLS handshake on the next request.
        private val sharedConnectionPool = okhttp3.ConnectionPool(
            maxIdleConnections = 5,
            keepAliveDuration = 5,
            TimeUnit.MINUTES
        )
    }

    private fun Message.toDto(): MessageDto = MessageDto(
        role = role.name.lowercase(),
        content = listOf(ContentPart.Text(text = content))
    )

    private fun Message.toDto(remoteAttachments: List<RemoteAttachment>): MessageDto {
        val parts = mutableListOf<ContentPart>()
        if (content.isNotBlank()) parts.add(ContentPart.Text(text = content))
        remoteAttachments.forEach {
            parts.add(ContentPart.ImageUrl(imageUrl = ImageUrlData(url = it.url)))
        }
        if (parts.isEmpty()) parts.add(ContentPart.Text(text = ""))
        return MessageDto(role = role.name.lowercase(), content = parts)
    }
}
