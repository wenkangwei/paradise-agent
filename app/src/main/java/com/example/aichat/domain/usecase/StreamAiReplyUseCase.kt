package com.example.aichat.domain.usecase

import android.content.Context
import android.net.Uri
import com.example.aichat.data.provider.StreamEvent
import com.example.aichat.data.remote.ApiException
import com.example.aichat.data.remote.AttachmentEncoder
import com.example.aichat.data.remote.ChatRemoteDataSource
import com.example.aichat.data.remote.NetworkException
import com.example.aichat.data.repository.ApiProfileRepository
import com.example.aichat.domain.model.Message
import com.example.aichat.domain.model.MessageMetadata
import com.example.aichat.domain.model.MessageStatus
import com.example.aichat.domain.model.Role
import com.example.aichat.domain.repository.ChatRepository
import com.example.aichat.ui.chat.model.Attachment
import dagger.hilt.android.qualifiers.ApplicationContext
import java.util.UUID
import javax.inject.Inject
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.withContext

/**
 * Streams an AI reply in a process-agnostic way.
 *
 * This use-case is invoked both from the UI ViewModel and from the
 * [com.example.aichat.service.StreamingService] background process. It handles:
 *   - persisting user attachments
 *   - inserting the user message and the AI placeholder row
 *   - running the SSE request
 *   - incrementally persisting content/reasoning every 500ms
 *   - finalising the placeholder to COMPLETE / INTERRUPTED
 *   - generating a conversation title on the first exchange
 *
 * Keeping this logic inside a use-case lets the streaming survive Activity
 * recreation and even process restarts when combined with a foreground service.
 */
class StreamAiReplyUseCase @Inject constructor(
    private val repository: ChatRepository,
    private val remoteDataSource: ChatRemoteDataSource,
    private val apiProfileRepo: ApiProfileRepository,
    @ApplicationContext private val context: Context
) {

    /**
     * Execute the streaming reply.
     *
     * @param conversationId target conversation id (must already exist)
     * @param text raw user text
     * @param attachments UI attachments picked by the user; will be persisted to disk
     * @param onError optional callback invoked when a non-fatal API/network error occurs
     */
    suspend operator fun invoke(
        conversationId: String,
        text: String,
        attachments: List<Attachment>,
        aiMessageId: String,
        onError: suspend (String) -> Unit = {}
    ) {
        val trimmed = text.trim()

        try {
            val now = System.currentTimeMillis()

            // 1. Persist attachments to filesDir and rewrite URIs to absolute paths.
            val persistedAttachments = if (attachments.isNotEmpty()) {
                attachments.map { uiAtt ->
                    val filePath = AttachmentEncoder.persist(context, Uri.parse(uiAtt.uri))
                    uiAtt.copy(uri = filePath)
                }
            } else emptyList()

            val domainAttachments = persistedAttachments.map { uiAtt ->
                com.example.aichat.domain.model.Attachment(
                    id = uiAtt.id,
                    mimeType = uiAtt.mimeType,
                    uri = uiAtt.uri
                )
            }

            // 2. Insert the user message.
            val userMessage = Message(
                id = UUID.randomUUID().toString(),
                conversationId = conversationId,
                role = Role.USER,
                content = trimmed,
                timestamp = now,
                attachments = domainAttachments
            )
            withContext(NonCancellable) {
                repository.appendMessage(userMessage)
            }

            // 3. Insert the AI placeholder row up-front so a crash mid-stream
            //    still leaves a partial reply on disk. The id is provided by the
            //    caller so the optimistic UI placeholder and the Room row share
            //    the same id — no duplicate row when the service writes back.
            val aiPlaceholder = Message(
                id = aiMessageId,
                conversationId = conversationId,
                role = Role.ASSISTANT,
                content = "",
                timestamp = System.currentTimeMillis(),
                status = MessageStatus.STREAMING
            )
            withContext(NonCancellable) {
                repository.appendMessage(aiPlaceholder)
            }

            // 4. Build history and run the SSE request.
            val fullHistory = repository.getMessages(conversationId)
            val hasAnyAttachments = fullHistory.any { it.attachments.isNotEmpty() }

            val stream = if (hasAnyAttachments) {
                val multimodalMessages = buildMultimodalPayload(fullHistory)
                remoteDataSource.streamChatMultimodal(multimodalMessages)
            } else {
                remoteDataSource.streamChat(fullHistory)
            }

            val contentBuilder = StringBuilder()
            val reasoningBuilder = StringBuilder()
            var finishedNormally: Boolean? = null
            var lastSaveMs = System.currentTimeMillis()
            // Populated when the SSE request itself fails (HTTP 4xx/5xx, network
            // drop, etc.). Finalisation persists this into the placeholder so the
            // UI shows a real error bubble instead of spinning forever.
            var apiError: String? = null
            var errorCategory: String? = null

            try {
                stream.collect { event ->
                    when (event) {
                        is StreamEvent.ContentDelta -> {
                            contentBuilder.append(event.text)
                            maybePersistStreaming(
                                aiMessageId,
                                contentBuilder,
                                reasoningBuilder,
                                lastSaveMs
                            ) { newLast -> lastSaveMs = newLast }
                        }
                        is StreamEvent.ReasoningDelta -> {
                            reasoningBuilder.append(event.text)
                            maybePersistStreaming(
                                aiMessageId,
                                contentBuilder,
                                reasoningBuilder,
                                lastSaveMs
                            ) { newLast -> lastSaveMs = newLast }
                        }
                        is StreamEvent.Finish -> {
                            finishedNormally = true
                        }
                        is StreamEvent.ToolCall -> { /* reserved */ }
                        is StreamEvent.Cancelled -> {
                            finishedNormally = false
                        }
                    }
                }
            } catch (e: kotlinx.coroutines.CancellationException) {
                finishedNormally = false
            } catch (e: ApiException) {
                apiError = "API 错误：${e.message}"
                errorCategory = "api"
                onError(apiError!!)
            } catch (e: NetworkException) {
                apiError = "网络错误：${e.message}"
                errorCategory = "network"
                onError(apiError!!)
            } catch (e: Exception) {
                apiError = "未知错误：${e.message}"
                errorCategory = "unknown"
                onError(apiError!!)
            }

            // 5. Finalise the placeholder row.
            val finalContent = if (apiError != null) {
                "⚠️ $apiError"
            } else {
                contentBuilder.toString()
            }
            val finalReasoning = if (apiError != null) null else reasoningBuilder.toString().ifBlank { null }
            val status = when {
                apiError != null -> MessageStatus.FAILED
                finishedNormally == true -> MessageStatus.COMPLETE
                finishedNormally == false -> MessageStatus.INTERRUPTED
                else -> MessageStatus.COMPLETE
            }
            if (finalContent.isNotBlank() || finalReasoning != null) {
                withContext(NonCancellable) {
                    runCatching {
                        repository.updateStreamingMessage(
                            id = aiMessageId,
                            content = finalContent,
                            reasoningContent = finalReasoning,
                            status = status.name
                        )
                        when {
                            apiError != null -> repository.updateMessageMetadata(
                                aiMessageId,
                                MessageMetadata(
                                    interruptedReason = apiError,
                                    errorCategory = errorCategory
                                )
                            )
                            status == MessageStatus.INTERRUPTED -> repository.updateMessageMetadata(
                                aiMessageId,
                                MessageMetadata(interruptedReason = "user_cancelled")
                            )
                        }
                        // Don't touch conversation lastMessage with the error
                        // text — keep the user's prompt as the latest entry so
                        // the conversation list preview stays meaningful.
                        if (apiError == null) {
                            repository.touchConversation(
                                conversationId,
                                finalContent.take(100),
                                System.currentTimeMillis()
                            )
                        }
                    }
                }
            } else {
                withContext(NonCancellable) {
                    runCatching { repository.deleteMessage(aiMessageId) }
                }
            }

            // 6. Title generation on first exchange.
            if (status == MessageStatus.COMPLETE) {
                generateTitleIfNeeded(conversationId, trimmed)
            }
        } catch (e: ApiException) {
            onError("API error: ${e.message}")
        } catch (e: NetworkException) {
            onError("Network error: ${e.message}")
        } catch (e: Exception) {
            onError("Unexpected error: ${e.message}")
        }
    }

    private suspend fun maybePersistStreaming(
        aiMessageId: String,
        contentBuilder: StringBuilder,
        reasoningBuilder: StringBuilder,
        lastSaveMs: Long,
        updateLastSave: (Long) -> Unit
    ) {
        if (System.currentTimeMillis() - lastSaveMs < SAVE_INTERVAL_MS) return
        updateLastSave(System.currentTimeMillis())
        val snapContent = contentBuilder.toString()
        val snapReasoning = reasoningBuilder.toString().ifBlank { null }
        runCatching {
            withContext(NonCancellable) {
                repository.updateStreamingMessage(
                    id = aiMessageId,
                    content = snapContent,
                    reasoningContent = snapReasoning,
                    status = MessageStatus.STREAMING.name
                )
            }
        }
    }

    private suspend fun generateTitleIfNeeded(conversationId: String, firstUserMessage: String) {
        val messages = runCatching { repository.getMessages(conversationId) }
            .getOrDefault(emptyList())
        if (messages.count { it.role == Role.USER } > 1) return

        val system = Message(
            id = UUID.randomUUID().toString(),
            conversationId = "",
            role = Role.SYSTEM,
            content = "用不超过15个中文字符概括下面用户输入的主题。直接输出标题文字，" +
                "不要引号、不要标点、不要任何前缀（例如『标题：』）。",
            timestamp = System.currentTimeMillis()
        )
        val user = Message(
            id = UUID.randomUUID().toString(),
            conversationId = "",
            role = Role.USER,
            content = firstUserMessage,
            timestamp = System.currentTimeMillis()
        )
        val builder = StringBuilder()
        val title = try {
            remoteDataSource.streamChat(listOf(system, user)).collect { event ->
                if (event is StreamEvent.ContentDelta) builder.append(event.text)
            }
            val cleaned = builder.toString()
                .trim()
                .lines()
                .joinToString("")
                .replace("\"", "")
                .replace("「", "")
                .replace("」", "")
                .take(15)
            if (cleaned.isBlank()) firstUserMessage.trim().take(15) else cleaned
        } catch (e: Exception) {
            firstUserMessage.trim().take(15)
        }
        runCatching { repository.renameConversation(conversationId, title) }
    }

    private suspend fun buildMultimodalPayload(
        fullHistory: List<Message>
    ): List<Pair<Message, List<com.example.aichat.data.remote.Attachment>>> {
        val recent = fullHistory.takeLast(MAX_MULTIMODAL_HISTORY)
        return recent.map { msg ->
            val remoteAttachments = if (msg.attachments.isEmpty()) emptyList()
            else msg.attachments.map { domainAtt ->
                val dataUrl = AttachmentEncoder.toDataUrl(domainAtt.uri)
                com.example.aichat.data.remote.Attachment(
                    url = dataUrl,
                    mimeType = domainAtt.mimeType
                )
            }
            msg to remoteAttachments
        }
    }

    private companion object {
        // Streaming persistence cadence. Lower = smoother perceived streaming
        // (the UI mirrors these snapshots into the optimistic placeholder) at
        // the cost of more single-row UPDATEs.
        //
        // Benchmarks (2026-07) show provider token gaps are typically 0-20ms
        // once streaming starts, so a 180ms cadence still reads as "bursty".
        // 80ms (~12 Hz) is smooth enough to look like continuous typing
        // while keeping DB write load trivial — single-row PK UPDATEs are
        // ~1-2ms each, so even at 12 Hz we burn <2% of one core.
        const val SAVE_INTERVAL_MS = 80L
        const val MAX_MULTIMODAL_HISTORY = 10
    }
}
