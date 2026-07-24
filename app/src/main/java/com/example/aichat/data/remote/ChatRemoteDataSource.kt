package com.example.aichat.data.remote

import com.example.aichat.data.remote.dto.ChatRequestDto
import com.example.aichat.data.remote.dto.ContentPart
import com.example.aichat.data.remote.dto.ImageUrlData
import com.example.aichat.data.remote.dto.MessageDto
import com.example.aichat.domain.model.Message
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.FlowCollector
import kotlinx.coroutines.flow.flow
import retrofit2.HttpException
import java.io.IOException
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
 * Converts domain [Message] list to DTOs, calls the streaming endpoint,
 * and returns a [Flow] of content token strings.
 */
@Singleton
class ChatRemoteDataSource @Inject constructor(
    private val apiService: AiApiService,
    private val streamClient: AiStreamClient,
    private val configManager: ConfigManager
) {

    /**
     * Streams a chat completion, emitting content token deltas.
     *
     * @param messages conversation messages (text-only)
     * @param model optional model override; falls back to [ConfigManager] default
     */
    fun streamChat(
        messages: List<Message>,
        model: String? = null
    ): Flow<String> = flow {
        val config = configManager.currentConfig()
        val request = ChatRequestDto(
            model = model ?: config.model,
            messages = messages.map { it.toDto() },
            stream = true
        )
        executeStream(request)
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
    ): Flow<String> = flow {
        val config = configManager.currentConfig()
        val request = ChatRequestDto(
            model = model ?: config.model,
            messages = messages.map { (msg, attachments) -> msg.toDto(attachments) },
            stream = true
        )
        executeStream(request)
    }

    /**
     * Builds the streaming request, handles errors, and emits tokens.
     * Called within a [flow] builder scope.
     */
    private suspend fun FlowCollector<String>.executeStream(
        request: ChatRequestDto
    ) {
        val responseBody = try {
            apiService.streamChat(request)
        } catch (e: HttpException) {
            throw ApiException("API error: HTTP ${e.code()} - ${e.message()}", e)
        } catch (e: IOException) {
            throw NetworkException("Network error: ${e.message}", e)
        }

        streamClient.toTokenFlow(responseBody).collect { token ->
            emit(token)
        }
    }

    /**
     * Converts a domain [Message] to a text-only [MessageDto].
     */
    private fun Message.toDto(): MessageDto = MessageDto(
        role = role.name.lowercase(),
        content = listOf(ContentPart.Text(text = content))
    )

    /**
     * Converts a domain [Message] with [attachments] to a multimodal [MessageDto].
     * Text part first, followed by image parts.
     */
    private fun Message.toDto(attachments: List<Attachment>): MessageDto {
        val parts = mutableListOf<ContentPart>()

        if (content.isNotBlank()) {
            parts.add(ContentPart.Text(text = content))
        }

        attachments.forEach { att ->
            parts.add(
                ContentPart.ImageUrl(imageUrl = ImageUrlData(url = att.url))
            )
        }

        if (parts.isEmpty()) {
            parts.add(ContentPart.Text(text = ""))
        }

        return MessageDto(role = role.name.lowercase(), content = parts)
    }
}
