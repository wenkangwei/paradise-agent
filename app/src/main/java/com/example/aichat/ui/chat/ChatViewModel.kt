package com.example.aichat.ui.chat

import android.content.Context
import android.net.Uri
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.example.aichat.data.provider.StreamEvent
import com.example.aichat.data.remote.ApiException
import com.example.aichat.data.remote.AttachmentEncoder
import com.example.aichat.data.remote.ChatRemoteDataSource
import com.example.aichat.data.remote.NetworkException
import com.example.aichat.data.repository.ApiProfileRepository
import com.example.aichat.di.ApplicationScope
import com.example.aichat.domain.model.Message
import com.example.aichat.domain.model.MessageMetadata
import com.example.aichat.domain.model.MessageStatus
import com.example.aichat.domain.model.Role
import com.example.aichat.domain.repository.ChatRepository
import com.example.aichat.service.StreamingService
import com.example.aichat.service.StreamingWakeLock
import com.example.aichat.ui.chat.model.Attachment
import com.example.aichat.ui.chat.model.ChatMessage
import com.example.aichat.ui.chat.model.toChatMessages
import dagger.hilt.android.lifecycle.HiltViewModel
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import kotlinx.coroutines.CoroutineExceptionHandler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.UUID

@HiltViewModel
class ChatViewModel @Inject constructor(
    private val repository: ChatRepository,
    private val remoteDataSource: ChatRemoteDataSource,
    private val apiProfileRepo: ApiProfileRepository,
    @ApplicationContext private val context: Context,
    @ApplicationScope private val appScope: CoroutineScope,
    private val streamingWakeLock: StreamingWakeLock
) : ViewModel() {

    private val _uiState = MutableStateFlow(ChatUiState())
    val uiState: StateFlow<ChatUiState> = _uiState.asStateFlow()

    private val _events = MutableSharedFlow<ChatEvent>(extraBufferCapacity = 5)
    val events: SharedFlow<ChatEvent> = _events.asSharedFlow()

    private var streamingJob: Job? = null
    private var messagesObserverJob: Job? = null
    private val applicationScope: CoroutineScope
        get() = appScope

    /**
     * Cap on how many recent history messages get their attachments re-encoded
     * as base64 for a multimodal request. Encoding the entire history at once
     * OOMs the app on long image conversations; this keeps the request bounded
     * while still giving the model enough context.
     */
    private companion object {
        const val MAX_MULTIMODAL_HISTORY = 10
    }

    /** Catches unhandled exceptions in the streaming coroutine so a single
     *  malformed SSE chunk or state race doesn't kill the app. */
    private val exceptionHandler = CoroutineExceptionHandler { _, throwable ->
        _uiState.update { it.copy(isLoading = false, isStreaming = false) }
        removeStreamingPlaceholder()
        _events.tryEmit(ChatEvent.ShowError(
            "发生未知错误: ${throwable.message?.take(80) ?: "请重试"}", null
        ))
    }

    init {
        observeConversations()
        observeApiProfiles()
    }

    override fun onCleared() {
        super.onCleared()
        // Intentionally NOT cancelling streamingJob here - it runs in
        // applicationScope and must survive Activity recreation (screen rotate,
        // lock/unlock). The service keeps the process alive; the job cleans
        // itself up when the stream finishes.
        releaseWakeLock()
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
                    // Sync currentConversationTitle from the up-to-date conversation row
                    // (covers both external rename and our LLM-generated title write-back).
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

    private fun observeMessages(conversationId: String) {
        messagesObserverJob?.cancel()
        messagesObserverJob = viewModelScope.launch {
            repository.observeMessages(conversationId).collect { messages ->
                val hasStreaming = messages.any { it.status == MessageStatus.STREAMING }
                _uiState.update { state ->
                    if (state.isStreaming && hasStreaming) {
                        // The streaming coroutine in applicationScope is driving
                        // the UI directly; skip Room updates to avoid cursor jumps.
                        state
                    } else {
                        state.copy(
                            messages = messages.toChatMessages(),
                            isStreaming = hasStreaming
                        )
                    }
                }
            }
        }
    }

    /**
     * Send a user message with optional image attachments and stream the AI response.
     *
     * The stream emits [StreamEvent]s — content deltas are appended to the
     * visible message, reasoning deltas are surfaced separately so the UI can
     * render them in a collapsible section (Phase 2 will wire that UI),
     * and the final status is persisted as [MessageStatus.INTERRUPTED] if the
     * user cancelled mid-stream (partial content is still saved).
     */
    fun sendMessage(text: String, attachments: List<Attachment> = emptyList()) {
        val trimmed = text.trim()
        val sendAttachments = attachments.ifEmpty { _uiState.value.pendingAttachments }
        val hasContent = trimmed.isNotBlank() || sendAttachments.isNotEmpty()
        if (!hasContent || _uiState.value.isLoading) return

        _uiState.update { it.copy(pendingAttachments = emptyList()) }

        streamingJob = applicationScope.launch(exceptionHandler) {
            _uiState.update { it.copy(isLoading = true, error = null) }
            streamingWakeLock.acquire(context)
            StreamingService.start(context)

            try {
                val conversationId = _uiState.value.currentConversationId
                    ?: repository.createConversation().also { newId ->
                        _uiState.update { it.copy(currentConversationId = newId) }
                    }

                val now = System.currentTimeMillis()

                // Persist attachments to app-internal storage (filesDir/attachments/)
                // and store ONLY the file path in Room. This avoids the 2 MB CursorWindow
                // cap that bit us when we stored base64 data URLs inline.
                // Also rewrite the UI attachment URIs to the file paths so the displayed
                // message bubble renders from the same file the DB references.
                val persistedAttachments = if (sendAttachments.isNotEmpty()) {
                    sendAttachments.map { uiAtt ->
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

                val userMessage = Message(
                    id = UUID.randomUUID().toString(),
                    conversationId = conversationId,
                    role = Role.USER,
                    content = trimmed,
                    timestamp = now,
                    attachments = domainAttachments
                )
                repository.appendMessage(userMessage)

                val userChatMessage = ChatMessage(
                    id = userMessage.id,
                    role = com.example.aichat.ui.chat.model.Role.USER,
                    content = trimmed,
                    attachments = persistedAttachments
                )

                val currentMessages = repository.getMessages(conversationId)
                val chatMessages = currentMessages.toChatMessages().map { msg ->
                    if (msg.id == userMessage.id) userChatMessage else msg
                }

                val aiMessageId = UUID.randomUUID().toString()
                val streamingAiMessage = ChatMessage(
                    id = aiMessageId,
                    role = com.example.aichat.ui.chat.model.Role.ASSISTANT,
                    content = "",
                    isStreaming = true
                )

                _uiState.update {
                    it.copy(
                        messages = chatMessages + streamingAiMessage,
                        isStreaming = true
                    )
                }

                val fullHistory = repository.getMessages(conversationId)
                val hasAnyAttachments = fullHistory.any { it.attachments.isNotEmpty() }

                val contentBuilder = StringBuilder()
                val reasoningBuilder = StringBuilder()
                // null = unknown; true = Finish received; false = cancelled before Finish
                var finishedNormally: Boolean? = null

                // Persist the AI placeholder row up-front so a crash mid-stream
                // still leaves a partial reply on disk. Updated incrementally
                // every SAVE_INTERVAL_MS, then again on completion.
                val aiPlaceholder = Message(
                    id = aiMessageId,
                    conversationId = conversationId,
                    role = Role.ASSISTANT,
                    content = "",
                    timestamp = System.currentTimeMillis(),
                    status = MessageStatus.STREAMING
                )
                withContext(NonCancellable) {
                    runCatching { repository.appendMessage(aiPlaceholder) }
                }
                var lastSaveMs = System.currentTimeMillis()
                val saveIntervalMs = 500L

                suspend fun maybePersistStreaming() {
                    if (System.currentTimeMillis() - lastSaveMs < saveIntervalMs) return
                    lastSaveMs = System.currentTimeMillis()
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

                try {
                    val stream = if (hasAnyAttachments) {
                        // Convert file paths to base64 data URLs ONLY for the HTTP
                        // request body. This is ephemeral — the base64 string lives
                        // only in memory and is never persisted to Room.
                        val multimodalMessages = buildMultimodalPayload(fullHistory)
                        remoteDataSource.streamChatMultimodal(multimodalMessages)
                    } else {
                        remoteDataSource.streamChat(fullHistory)
                    }

                    stream.collect { event ->
                        when (event) {
                            is StreamEvent.ContentDelta -> {
                                contentBuilder.append(event.text)
                                updateStreamingContent(
                                    aiMessageId,
                                    contentBuilder.toString(),
                                    reasoningBuilder.toString()
                                )
                                maybePersistStreaming()
                            }
                            is StreamEvent.ReasoningDelta -> {
                                reasoningBuilder.append(event.text)
                                updateStreamingContent(
                                    aiMessageId,
                                    contentBuilder.toString(),
                                    reasoningBuilder.toString()
                                )
                                maybePersistStreaming()
                            }
                            is StreamEvent.Finish -> {
                                finishedNormally = true
                            }
                            is StreamEvent.ToolCall -> {
                                // Reserved for future tool-calling support
                            }
                            is StreamEvent.Cancelled -> {
                                finishedNormally = false
                            }
                        }
                    }
                } catch (e: kotlinx.coroutines.CancellationException) {
                    finishedNormally = false
                }

                // Persist AI message — partial content is saved even when interrupted
                val finalContent = contentBuilder.toString()
                val finalReasoning = reasoningBuilder.toString().ifBlank { null }
                val status = when (finishedNormally) {
                    true -> MessageStatus.COMPLETE
                    false -> MessageStatus.INTERRUPTED
                    null -> MessageStatus.COMPLETE // stream closed without explicit Finish
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
                            if (status == MessageStatus.INTERRUPTED) {
                                repository.updateMessageMetadata(
                                    aiMessageId,
                                    MessageMetadata(interruptedReason = "user_cancelled")
                                )
                            }
                            // Refresh conversation's lastMessage + updatedAt so
                            // the drawer shows the latest preview.
                            repository.touchConversation(
                                conversationId,
                                finalContent.take(100),
                                System.currentTimeMillis()
                            )
                        }
                    }
                } else {
                    // Empty reply - remove the placeholder so it doesn't linger.
                    withContext(NonCancellable) {
                        runCatching { repository.deleteMessage(aiMessageId) }
                    }
                }

                _uiState.update { it.copy(isStreaming = false) }
                observeMessages(conversationId)

                if (status == MessageStatus.COMPLETE) {
                    _events.emit(ChatEvent.MessageSent)
                    // Generate conversation title from the first user message —
                    // async, best-effort. Falls back to first 15 chars on any error.
                    val firstUserMessage = trimmed
                    val convId = conversationId
                    viewModelScope.launch(Dispatchers.IO) {
                        val messages = runCatching { repository.getMessages(convId) }
                            .getOrDefault(emptyList())
                        if (messages.count { it.role == Role.USER } <= 1) {
                            val title = generateTitle(firstUserMessage)
                            runCatching { repository.renameConversation(convId, title) }
                            _uiState.update { it.copy(currentConversationTitle = title) }
                        }
                    }
                }
            } catch (e: ApiException) {
                handleError(e.message ?: "API request failed", text, sendAttachments)
            } catch (e: NetworkException) {
                handleError(e.message ?: "Network error", text, sendAttachments)
            } catch (e: Exception) {
                handleError(e.message ?: "Unexpected error", text, sendAttachments, retryable = false)
            } finally {
                _uiState.update { it.copy(isLoading = false) }
                streamingWakeLock.release()
                StreamingService.stop(context)
            }
        }
    }

    /**
     * Hold a partial WakeLock while streaming so the CPU stays awake when the
     * screen turns off. Without this, Android Doze defers/suspends network
     * activity and the SSE connection silently dies, taking the partial AI
     * reply with it.
     *
     * Replaced by [StreamingWakeLock] + [StreamingService]; kept here as a
     * no-op marker for documentation.
     */
    private fun acquireWakeLock() {}

    private fun releaseWakeLock() {}

    private suspend fun handleError(
        message: String,
        originalText: String,
        originalAttachments: List<Attachment>,
        retryable: Boolean = true
    ) {
        _uiState.update { it.copy(error = message, isStreaming = false) }
        val retry: (() -> Unit)? = if (retryable) {
            { sendMessage(originalText, originalAttachments) }
        } else null
        _events.emit(ChatEvent.ShowError(message, retry))
        removeStreamingPlaceholder()
    }

    /**
     * Update the streaming AI message content/reasoning in the UI state.
     */
    private fun updateStreamingContent(aiMessageId: String, content: String, reasoning: String) {
        _uiState.update { state ->
            val updatedMessages = state.messages.map { msg ->
                if (msg.id == aiMessageId) {
                    msg.copy(content = content, isStreaming = true)
                } else msg
            }
            state.copy(messages = updatedMessages)
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

    private fun removeStreamingPlaceholder() {
        _uiState.update { state ->
            val cleaned = state.messages.filterNot { it.isStreaming && it.content.isEmpty() }
            state.copy(messages = cleaned)
        }
    }

    /**
     * User-initiated stop. Cancels the streaming job; partial content will be
     * saved by the cancelled coroutine's `withContext(NonCancellable)` block.
     */
    fun stopGenerating() {
        streamingJob?.cancel()
        _uiState.update { it.copy(isLoading = false) }
        releaseWakeLock()
        streamingWakeLock.release()
        StreamingService.stop(context)
    }

    fun newChat() {
        stopGenerating()
        messagesObserverJob?.cancel()
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

    fun selectConversation(conversationId: String) {
        stopGenerating()
        val title = _uiState.value.conversations
            .firstOrNull { it.id == conversationId }?.title
            ?.takeIf { it.isNotBlank() && it != "New Conversation" }
        _uiState.update {
            it.copy(
                currentConversationId = conversationId,
                currentConversationTitle = title,
                error = null,
                isLoading = false,
                isStreaming = false,
                pendingAttachments = emptyList()
            )
        }
        observeMessages(conversationId)
    }

    fun deleteConversation(conversationId: String) {
        viewModelScope.launch {
            try {
                repository.deleteConversation(conversationId)
                if (_uiState.value.currentConversationId == conversationId) {
                    newChat()
                }
            } catch (e: Exception) {
                _events.emit(ChatEvent.ShowError("Failed to delete conversation", null))
            }
        }
    }

    /**
     * Toggle AI-message feedback ("like" / "dislike"). Pressing the same reaction
     * again clears it. Optimistic UI update + background persist.
     */
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
     * Builds the multimodal payload for the LLM API.
     *
     * Memory: every attachment is base64-encoded into a single big string.
     * Encoding ALL history attachments at once used to OOM the app on 3+ image
     * conversations (each 5 MB image -> ~7 MB base64 -> multiplied across N
     * messages -> >100 MB string heap). We cap to the most recent
     * [MAX_MULTIMODAL_HISTORY] messages so memory stays bounded; older
     * context is dropped (acceptable - LLMs already lose long-context
     * fidelity well before this limit).
     */
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

    /**
     * Asks the active LLM to summarize the user's first message into ≤15 chars.
     * Reuses the streaming path — collect until the stream closes, concatenate
     * content deltas, strip whitespace/newlines, hard-truncate to 15 chars.
     * Any error → fallback to first 15 chars of the raw message.
     */
    private suspend fun generateTitle(firstUserMessage: String): String {
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
        return try {
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
    }

    fun loadConversations() {
        // No-op: conversations are already observed
    }

    fun clearError() {
        _uiState.update { it.copy(error = null) }
    }
}
