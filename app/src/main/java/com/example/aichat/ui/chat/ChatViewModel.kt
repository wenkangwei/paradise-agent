package com.example.aichat.ui.chat

import android.content.Context
import android.net.Uri
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.example.aichat.data.remote.AttachmentEncoder
import com.example.aichat.domain.model.Message
import com.example.aichat.domain.model.MessageStatus
import com.example.aichat.domain.model.Role
import com.example.aichat.domain.repository.ChatRepository
import com.example.aichat.service.StreamingService
import com.example.aichat.ui.chat.model.Attachment
import com.example.aichat.ui.chat.model.ChatMessage
import com.example.aichat.ui.chat.model.toChatMessages
import com.google.gson.Gson
import dagger.hilt.android.lifecycle.HiltViewModel
import dagger.hilt.android.qualifiers.ApplicationContext
import java.util.UUID
import javax.inject.Inject
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * ViewModel for the chat screen.
 *
 * v4.0 design — **Room is the single source of truth**:
 *   - No optimistic UI splicing: the messages list is always a snapshot of
 *     what Room currently has on disk for the active conversation.
 *   - `isStreaming` / `isLoading` are derived from the messages' status
 *     (`STREAMING` rows exist → true). The ViewModel never flips them
 *     manually, which avoids the prior "stop button causes UI to flash
 *     blank" race.
 *   - The `:streaming` process inserts the AI placeholder row within ~100ms
 *     of `sendMessage` and Room's multi-instance invalidation delivers it
 *     to [observeMessages] automatically.
 *
 * Multi-session concurrency:
 *   - The user can leave a thinking-model reply running in conversation A
 *     while they start a new chat in B. The drawer shows a green dot on
 *     any conversation that currently has a STREAMING row.
 *   - [stopGenerating] only stops the *current* conversation's stream.
 *   - [selectConversation] / [newChat] deliberately do NOT stop background
 *     streams — they keep running until they finish naturally or the user
 *     explicitly stops them.
 */
@HiltViewModel
class ChatViewModel @Inject constructor(
    private val repository: ChatRepository,
    private val apiProfileRepo: com.example.aichat.data.repository.ApiProfileRepository,
    @ApplicationContext private val context: Context
) : ViewModel() {

    private val _uiState = MutableStateFlow(ChatUiState())
    val uiState: StateFlow<ChatUiState> = _uiState.asStateFlow()

    private val _events = MutableSharedFlow<ChatEvent>(extraBufferCapacity = 5)
    val events: SharedFlow<ChatEvent> = _events.asSharedFlow()

    private var messagesObserverJob: Job? = null
    private val gson = Gson()

    init {
        observeConversations()
        observeApiProfiles()
        observeStreamingConversations()
        // One-time sweep of STREAMING rows orphaned by a previous :streaming
        // process that died mid-stream (app crashed, OEM killed, reboot).
        // Without this the drawer would show a green dot forever on dead
        // sessions. Run on first ViewModel init, not Application.onCreate,
        // so it only fires when the user actually opens the chat screen
        // (and only in the main process — not the :streaming process).
        viewModelScope.launch {
            runCatching { repository.markDanglingStreamingInterrupted("session_init") }
        }
    }

    private fun observeApiProfiles() {
        viewModelScope.launch {
            combine(apiProfileRepo.observeAll(), apiProfileRepo.observeActive()) { all, active ->
                _uiState.update { it.copy(profiles = all, activeProfile = active) }
            }.collect { /* state updated inside combine block */ }
        }
    }

    fun selectApiProfile(id: String) {
        viewModelScope.launch { apiProfileRepo.setActive(id) }
    }

    private fun observeConversations() {
        viewModelScope.launch {
            repository.observeConversations().collect { conversations ->
                _uiState.update { state ->
                    val currentTitle = state.currentConversationId?.let { id ->
                        conversations.firstOrNull { it.id == id }?.title
                            ?.takeIf { it.isNotBlank() && it != "New Conversation" }
                    }
                    state.copy(
                        conversations = conversations,
                        currentConversationTitle = currentTitle ?: state.currentConversationTitle
                    )
                }
            }
        }
    }

    /**
     * Tracks every conversation that currently has a STREAMING row in Room,
     * regardless of which one the user is currently viewing. Drives:
     *   - drawer green-dot indicators (visible from any conversation)
     *   - `MAX_CONCURRENT_STREAMS` cap in [sendMessage]
     */
    private fun observeStreamingConversations() {
        viewModelScope.launch {
            repository.observeStreamingConversationIds().collect { ids ->
                _uiState.update { it.copy(streamingConversationIds = ids) }
            }
        }
    }

    private fun observeMessages(conversationId: String) {
        messagesObserverJob?.cancel()
        messagesObserverJob = viewModelScope.launch {
            // Room is the single source of truth. We no longer splice in
            // optimistic UI placeholders — the :streaming process inserts
            // the AI placeholder row within ~100ms of `sendMessage`, and
            // multi-instance invalidation delivers it to this observer.
            // The mapper now derives `isStreaming` from the row's status,
            // so the streaming bubble renders correctly even after the
            // user switches sessions and comes back.
            repository.observeMessages(conversationId).collect { messages ->
                val hasStreaming = messages.any { it.status == MessageStatus.STREAMING }
                _uiState.update { state ->
                    // Guard against stale collectors: if the user has since
                    // switched to another conversation, this emission is
                    // obsolete and must NOT overwrite the current state.
                    if (state.currentConversationId != conversationId) return@collect
                    state.copy(
                        messages = messages.toChatMessages(),
                        isStreaming = hasStreaming,
                        // isLoading mirrors hasStreaming so the input bar's
                        // spinner matches reality.
                        isLoading = hasStreaming
                    )
                }
            }
        }
    }

    /**
     * Send a user message and ask the streaming service to produce an AI reply.
     *
     * v4.0 changes:
     *   - No optimistic UI splicing — Room is the single source of truth.
     *   - If the *current* conversation is already streaming, automatically
     *     stop it first (BUG-12) instead of refusing the send.
     *   - Honor `MAX_CONCURRENT_STREAMS` across conversations.
     */
    fun sendMessage(text: String, attachments: List<Attachment> = emptyList()) {
        val trimmed = text.trim()
        val sendAttachments = attachments.ifEmpty { _uiState.value.pendingAttachments }
        val hasContent = trimmed.isNotBlank() || sendAttachments.isNotEmpty()
        if (!hasContent) return

        val convId = _uiState.value.currentConversationId
        val isCurrentStreaming = convId != null && convId in _uiState.value.streamingConversationIds
        // If the user is re-sending in a conversation that is currently
        // streaming, cancel that stream first — the UseCase will finalize
        // the prior placeholder as INTERRUPTED via its NonCancellable block.
        // (Smart-cast to non-null is safe because `isCurrentStreaming`
        // already requires `convId != null`.)
        if (isCurrentStreaming) {
            StreamingService.stop(context, convId!!)
        } else if (_uiState.value.isLoading) {
            // Loading flag set but no active stream — likely a transient
            // race; ignore the send rather than risk stacking two streams.
            return
        }

        // Cap concurrent active streams across all conversations.
        val activeCount = _uiState.value.streamingConversationIds.size -
            (if (isCurrentStreaming) 1 else 0)
        if (activeCount >= MAX_CONCURRENT_STREAMS) {
            viewModelScope.launch {
                _events.emit(
                    ChatEvent.ShowError(
                        "同时最多 $MAX_CONCURRENT_STREAMS 个对话进行中，请先停止一个",
                        null
                    )
                )
            }
            return
        }

        _uiState.update { it.copy(pendingAttachments = emptyList(), isLoading = true, error = null) }

        viewModelScope.launch {
            try {
                val conversationId = _uiState.value.currentConversationId
                    ?: repository.createConversation().also { newId ->
                        _uiState.update { it.copy(currentConversationId = newId) }
                    }
                // Make sure we're observing this conversation so the
                // :streaming process's writes are reflected in the UI.
                if (messagesObserverJob == null) observeMessages(conversationId)

                // Persist attachments before handing them to the service.
                val persistedAttachments = if (sendAttachments.isNotEmpty()) {
                    sendAttachments.map { uiAtt ->
                        val filePath = AttachmentEncoder.persist(context, Uri.parse(uiAtt.uri))
                        uiAtt.copy(uri = filePath)
                    }
                } else emptyList()

                // Append the user message to Room — the service reads it
                // from history when building the LLM request.
                val userMessage = Message(
                    id = UUID.randomUUID().toString(),
                    conversationId = conversationId,
                    role = Role.USER,
                    content = trimmed,
                    timestamp = System.currentTimeMillis(),
                    attachments = persistedAttachments.map { uiAtt ->
                        com.example.aichat.domain.model.Attachment(
                            id = uiAtt.id,
                            mimeType = uiAtt.mimeType,
                            uri = uiAtt.uri
                        )
                    }
                )
                withContext(Dispatchers.IO) { repository.appendMessage(userMessage) }

                // The AI placeholder row is inserted by the :streaming
                // process's UseCase, which propagates this same id. When
                // the observer pulls the new row in, the UI updates
                // naturally — no optimistic splicing needed.
                val aiMessageId = UUID.randomUUID().toString()
                StreamingService.start(
                    context = context,
                    conversationId = conversationId,
                    text = trimmed,
                    attachmentsJson = gson.toJson(persistedAttachments),
                    aiMessageId = aiMessageId
                )
            } catch (e: Exception) {
                _events.emit(ChatEvent.ShowError(e.message ?: "启动失败", null))
                _uiState.update { it.copy(isLoading = false) }
            }
        }
    }

    fun addPendingAttachment(uri: String, mimeType: String) {
        val attachment = Attachment(
            id = UUID.randomUUID().toString(),
            uri = uri,
            mimeType = mimeType,
            displayName = Uri.parse(uri).lastPathSegment ?: "image"
        )
        _uiState.update { it.copy(pendingAttachments = it.pendingAttachments + attachment) }
    }

    fun removePendingAttachment(id: String) {
        _uiState.update {
            it.copy(pendingAttachments = it.pendingAttachments.filterNot { att -> att.id == id })
        }
    }

    fun clearPendingAttachments() {
        _uiState.update { it.copy(pendingAttachments = emptyList()) }
    }

    /**
     * User-initiated stop for the *current* conversation only. Other
     * conversations' streams keep running in the background.
     *
     * We only flip the input-bar spinner (`isLoading`); the message list
     * is left untouched and converges naturally once the :streaming
     * process writes INTERRUPTED to Room (typically 100-500ms later).
     * This avoids the previous "stop → UI flash to empty" race caused
     * by manually clearing `isStreaming` while Room still said STREAMING.
     */
    fun stopGenerating() {
        val convId = _uiState.value.currentConversationId ?: return
        StreamingService.stop(context, convId)
        _uiState.update { it.copy(isLoading = false) }
    }

    /**
     * Start a fresh conversation.
     *
     * v4.0: deliberately does NOT stop background streams — the user can
     * leave a thinking-model reply running in conversation A while they
     * start a new chat in B. The drawer's green-dot indicator keeps
     * them informed that A is still in progress.
     */
    fun newChat() {
        messagesObserverJob?.cancel()
        // CRITICAL: null out the reference after cancel. Job.cancel() only
        // marks the coroutine as cancelled — the reference itself is still
        // non-null, which trips the `if (messagesObserverJob == null)`
        // guard in sendMessage. Result: user sends a message in the fresh
        // chat, createConversation() assigns a new conversationId, but the
        // UI never observes that new conversation — so the user's message
        // and the AI's reply land in Room but never render until the user
        // manually clicks the new session in the drawer.
        messagesObserverJob = null
        _uiState.update {
            it.copy(
                messages = emptyList(),
                currentConversationId = null,
                currentConversationTitle = null,
                error = null,
                isLoading = false,
                isStreaming = false,
                pendingAttachments = emptyList()
            )
        }
    }

    /**
     * Switch to an existing conversation. v4.0: does NOT stop background
     * streams in other conversations — see [newChat] for the rationale.
     *
     * Does NOT call `markConversationStreamingInterrupted`: that conflicted
     * with the :streaming process's persist loop (status bounced between
     * INTERRUPTED and STREAMING). Instead, if a STREAMING row exists for
     * this conversation, the observer simply reflects it (green dot + the
     * streaming bubble continues to render where the :streaming process
     * left off).
     */
    fun selectConversation(conversationId: String) {
        messagesObserverJob?.cancel()
        val title = _uiState.value.conversations
            .firstOrNull { it.id == conversationId }?.title
            ?.takeIf { it.isNotBlank() && it != "New Conversation" }
        _uiState.update {
            it.copy(
                currentConversationId = conversationId,
                currentConversationTitle = title,
                error = null,
                pendingAttachments = emptyList()
                // Intentionally do NOT touch messages / isStreaming /
                // isLoading: observeMessages will emit the current Room
                // state immediately, which sets them correctly.
            )
        }
        observeMessages(conversationId)
    }

    fun deleteConversation(conversationId: String) {
        viewModelScope.launch {
            try {
                repository.deleteConversation(conversationId)
                // Stop any in-flight stream for this conversation so the
                // service doesn't keep writing to a deleted conversation
                // (FK cascade will have removed the rows anyway).
                StreamingService.stop(context, conversationId)
                if (_uiState.value.currentConversationId == conversationId) {
                    newChat()
                }
            } catch (e: Exception) {
                _events.emit(ChatEvent.ShowError("Failed to delete conversation", null))
            }
        }
    }

    fun setMessageReaction(messageId: String, reaction: String) {
        var newValue: String? = null
        _uiState.update { state ->
            val updated = state.messages.map { msg ->
                if (msg.id == messageId) {
                    newValue = if (msg.reaction == reaction) null else reaction
                    msg.copy(reaction = newValue)
                } else msg
            }
            state.copy(messages = updated)
        }
        viewModelScope.launch {
            runCatching { repository.setMessageReaction(messageId, newValue) }
        }
    }

    /**
     * Re-issue the user prompt that produced a FAILED AI reply. The failed
     * row is overwritten in place because we pass the same [failedAiMessageId]
     * back to the streaming service — the UseCase's `appendMessage(aiPlaceholder)`
     * uses OnConflictStrategy.REPLACE, so the new stream picks up where the
     * old one died without leaving a stale error bubble in history.
     *
     * Look-up happens against the in-memory `uiState.messages` snapshot of
     * the *current* conversation — no DB round-trip needed. The previous
     * user message's content and attachments are reused verbatim.
     */
    fun retryMessage(failedAiMessageId: String) {
        val state = _uiState.value
        val convId = state.currentConversationId ?: return
        val messages = state.messages
        val failedIdx = messages.indexOfFirst { it.id == failedAiMessageId }
        if (failedIdx < 0) return
        val failedMsg = messages[failedIdx]
        if (failedMsg.status != MessageStatus.FAILED) return

        // Find the user prompt that triggered this AI reply — walk backwards
        // from the failed bubble to the most recent USER message.
        // (ChatMessage.role is the UI-layer enum, distinct from domain.Role.)
        val prevUser = (failedIdx - 1 downTo 0)
            .mapNotNull { messages.getOrNull(it) }
            .firstOrNull { it.role == com.example.aichat.ui.chat.model.Role.USER } ?: return

        // Same concurrency semantics as [sendMessage]: if this conversation
        // already has a stream in flight (e.g. user retried while another
        // session's reply is running elsewhere), stop it first.
        val isCurrentStreaming = convId in state.streamingConversationIds
        if (isCurrentStreaming) {
            StreamingService.stop(context, convId)
        }
        val activeCount = state.streamingConversationIds.size -
            (if (isCurrentStreaming) 1 else 0)
        if (activeCount >= MAX_CONCURRENT_STREAMS) {
            viewModelScope.launch {
                _events.emit(
                    ChatEvent.ShowError(
                        "同时最多 $MAX_CONCURRENT_STREAMS 个对话进行中，请先停止一个",
                        null
                    )
                )
            }
            return
        }

        _uiState.update { it.copy(isLoading = true, error = null) }

        viewModelScope.launch {
            try {
                val attachmentsJson = gson.toJson(prevUser.attachments)
                StreamingService.start(
                    context = context,
                    conversationId = convId,
                    text = prevUser.content,
                    attachmentsJson = attachmentsJson,
                    aiMessageId = failedAiMessageId
                )
            } catch (e: Exception) {
                _events.emit(ChatEvent.ShowError(e.message ?: "重试失败", null))
                _uiState.update { it.copy(isLoading = false) }
            }
        }
    }

    fun loadConversations() {
        // No-op: conversations are already observed
    }

    fun clearError() {
        _uiState.update { it.copy(error = null) }
    }

    override fun onCleared() {
        // Do NOT stop the streaming service here - it runs in the :streaming
        // process and must survive Activity recreation.
        super.onCleared()
    }

    private companion object {
        /**
         * Hard cap on concurrently active streams. Reasoning:
         *   - Each SSE connection holds ~1MB of buffers.
         *   - Most LLM providers cap concurrent streams per API key
         *     (glm/kimi: 5-10); going beyond invites 429s.
         *   - Five parallel chats is well past any realistic usage; if
         *     the user really needs more they can stop one.
         */
        const val MAX_CONCURRENT_STREAMS = 5
    }
}
