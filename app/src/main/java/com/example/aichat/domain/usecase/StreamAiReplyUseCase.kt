package com.example.aichat.domain.usecase

import android.content.Context
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
import com.example.aichat.util.HonorOemHelper
import dagger.hilt.android.qualifiers.ApplicationContext
import java.util.UUID
import javax.inject.Inject
import kotlinx.coroutines.NonCancellable
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
            // NOTE: The user message is already persisted by ChatViewModel
            // BEFORE the service starts (so the optimistic UI has the row
            // and the service can read it from history). Do NOT re-insert it
            // here — that was duplicating every user message with a fresh
            // UUID and producing "message appears twice" on session re-entry.
            //
            // Likewise, attachments are already persisted to filesDir by the
            // ViewModel; filesDir is shared across the main and :streaming
            // processes of the same app, so no re-persist is needed.

            // 1. Sweep any orphaned STREAMING rows for this conversation.
            //    If a previous run was killed before finalising, its placeholder
            //    would still carry status=STREAMING and confuse the UI observer
            //    (which would think a stream is still in flight and refuse to
            //    render the new one). Mark them INTERRUPTED up-front.
            runCatching {
                repository.markConversationStreamingInterrupted(
                    conversationId,
                    "superseded_by_new_request"
                )
            }

            // 2. Insert the AI placeholder row up-front so a crash mid-stream
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

            // 3. Build history and run the SSE request.
            // Exclude the assistant placeholder we just inserted — its
            // content is empty and placing an empty assistant message after
            // the user's latest message violates ChatML turn order, which
            // causes some backends (Ollama qwen2.5) to emit an empty
            // response and terminate the stream immediately.
            val fullHistory = repository.getMessages(conversationId)
                .filterNot { msg -> msg.id == aiMessageId && msg.content.isBlank() }
            val hasAnyAttachments = fullHistory.any { it.attachments.isNotEmpty() }

            val stream = if (hasAnyAttachments) {
                val multimodalMessages = buildMultimodalPayload(fullHistory)
                remoteDataSource.streamChatMultimodal(multimodalMessages)
            } else {
                remoteDataSource.streamChat(fullHistory)
            }

            val contentBuilder = StringBuilder()
            val reasoningBuilder = StringBuilder()
            val searchResults = mutableListOf<com.example.aichat.domain.model.MessageMetadata.SearchResult>()
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
                        is StreamEvent.ToolCards -> {
                            searchResults.addAll(event.cards.flatMap { card ->
                                card.results?.map { r ->
                                    com.example.aichat.domain.model.MessageMetadata.SearchResult(
                                        title = r.title, snippet = r.snippet, url = r.url)
                                } ?: emptyList()
                            })
                        }
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
            //
            // v4.2.5: NEVER delete the AI placeholder row. The previous
            // `else { deleteMessage(aiMessageId) }` branch was the source
            // of the recurring "AI bubble disappears after lock screen"
            // regression. When the :streaming process is killed by Android
            // (Doze / OEM killer / memory pressure) while the model is still
            // in its thinking phase — before the first content token — the
            // CancellationException path lands here with empty
            // `contentBuilder` AND empty `reasoningBuilder`. The old code
            // would then DELETE the row, making the streaming bubble the
            // user saw a second ago vanish from the chat on next open.
            //
            // Now: always UPDATE. If we have no content AND no reasoning AND
            // no error message to surface, write a visible placeholder so
            // the bubble remains in the conversation history — the user can
            // see "this reply was interrupted" and tap retry, instead of
            // wondering where the message went.
            //
            // v4.2.11: Detect Honor PGManager-induced interruptions.
            // Signature: finishedNormally == null (no Finish/Cancelled event)
            // AND we did emit some content/reasoning. This means the SSE flow
            // was cut silently — on Honor devices that's almost always
            // PGManager freezing the process and destroying the socket
            // ~1.3s after screen-off (proven via logcat, see HonorOemHelper).
            // Tag such messages with errorCategory = "honor_oem_kill" so the
            // UI can show an actionable "open Honor settings" affordance.
            val likelyHonorKilled = finishedNormally == null &&
                (contentBuilder.isNotEmpty() || reasoningBuilder.isNotEmpty()) &&
                HonorOemHelper.isHonorOrHuawei()

            val finalContent = when {
                apiError != null -> "⚠️ $apiError"
                contentBuilder.isNotEmpty() -> contentBuilder.toString()
                // No content but reasoning exists: surface a visible marker
                // so the bubble remains in the chat list even when the render
                // guard in MessageBubble requires non-blank content (v4.2.8:
                // previously "" here, combined with v4.2.2 SSE retry silently
                // ending the flow on lock-screen socket abort, caused thinking
                // models' reply bubble to disappear after unlock).
                reasoningBuilder.isNotEmpty() -> "（已中断，请重试）"
                // Truly nothing was emitted: surface a visible marker so the
                // bubble isn't invisible in the chat list.
                else -> "（已中断，请重试）"
            }
            val finalReasoning = if (apiError != null) null else reasoningBuilder.toString().ifBlank { null }
            // v4.2.11: previously, finishedNormally==null fell through to
            // COMPLETE, which silently mislabeled Honor-killed partial
            // replies as fully complete. Now treat null as INTERRUPTED
            // whenever we have content/reasoning — the user gets a clear
            // signal that the reply was cut.
            val status = when {
                apiError != null -> MessageStatus.FAILED
                finishedNormally == true -> MessageStatus.COMPLETE
                finishedNormally == false -> MessageStatus.INTERRUPTED
                // finishedNormally == null: stream ended abnormally
                // (no Finish event, no Cancelled, no exception caught).
                // On Honor this is the PGManager signature.
                contentBuilder.isEmpty() && reasoningBuilder.isEmpty() -> MessageStatus.COMPLETE
                else -> MessageStatus.INTERRUPTED
            }
            val interruptedReason = when {
                apiError != null -> apiError
                finishedNormally == false -> "interrupted"
                likelyHonorKilled -> "honor_oem_kill"
                finishedNormally == null &&
                    (contentBuilder.isNotEmpty() || reasoningBuilder.isNotEmpty())
                    -> "stream_silent_end"
                else -> null
            }
            val honorTaggedCategory = if (likelyHonorKilled) "honor_oem_kill" else errorCategory
            withContext(NonCancellable) {
                runCatching {
                    repository.updateStreamingMessage(
                        id = aiMessageId,
                        content = finalContent,
                        reasoningContent = finalReasoning,
                        status = status.name
                    )
                    // Persist metadata (search results and/or error info)
                    if (interruptedReason != null || searchResults.isNotEmpty()) {
                        repository.updateMessageMetadata(
                            aiMessageId,
                            MessageMetadata(
                                interruptedReason = interruptedReason,
                                errorCategory = honorTaggedCategory,
                                searchResults = searchResults
                            )
                        )
                    }
                    // Don't touch conversation lastMessage with the error
                    // text — keep the user's prompt as the latest entry so
                    // the conversation list preview stays meaningful.
                    if (apiError == null && finalContent.isNotBlank() &&
                        finalContent != "（已中断，请重试）"
                    ) {
                        repository.touchConversation(
                            conversationId,
                            finalContent.take(100),
                            System.currentTimeMillis()
                        )
                    }
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
        // Pre-check: if an external sweep (app restart, dangling cleanup,
        // session re-entry) has already marked this row INTERRUPTED/FAILED,
        // we must NOT overwrite that with STREAMING again — that was the
        // source of BUG-3 / BUG-13 (status bouncing between INTERRUPTED
        // and STREAMING on every persist tick). Bail out cleanly; the
        // collector loop's CancellationException path will finalize.
        val currentStatus = runCatching { repository.getMessageStatus(aiMessageId) }
            .getOrNull()
        if (currentStatus == "INTERRUPTED" || currentStatus == "FAILED") {
            throw kotlinx.coroutines.CancellationException("interrupted_by_external_sweep")
        }
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

        // Wrap in NonCancellable: if the parent streamingJob is cancelled
        // mid-stream (user pressed stop), the partial reply is already
        // finalized but we still want the title to be generated so the
        // drawer shows something meaningful instead of "New Conversation".
        withContext(NonCancellable) {
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
        // Streaming persistence cadence. v4.0: lowered from 80ms (~12 Hz)
        // to 150ms (~7 Hz). The previous 80ms cadence combined with three
        // UPDATEs per tick (content + reasoning + status) was causing
        // "bursty" token delivery on low-end devices because the SSE
        // reader got back-pressured while Room wrote. 150ms is still
        // smooth enough to read as continuous typing (the cursor animation
        // bridges any visible gaps) while halving DB write load.
        const val SAVE_INTERVAL_MS = 150L
        const val MAX_MULTIMODAL_HISTORY = 10
    }
}
