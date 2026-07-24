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
import com.example.aichat.domain.model.Message
import com.example.aichat.domain.model.MessageMetadata
import com.example.aichat.domain.model.MessageStatus
import com.example.aichat.domain.model.Role
import com.example.aichat.domain.repository.ChatRepository
import com.example.aichat.ui.chat.model.Attachment
import com.example.aichat.ui.chat.model.ChatMessage
import com.example.aichat.ui.chat.model.toChatMessages
import dagger.hilt.android.lifecycle.HiltViewModel
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
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
    @ApplicationContext private val context: Context
) : ViewModel() {

    private val _uiState = MutableStateFlow(ChatUiState())
    val uiState: StateFlow<ChatUiState> = _uiState.asStateFlow()

    private val _events = MutableSharedFlow<ChatEvent>(extraBufferCapacity = 5)
    val events: SharedFlow<ChatEvent> = _events.asSharedFlow()

    private var streamingJob: Job? = null
    private var messagesObserverJob: Job? = null

    init {
        observeConversations()
        observeApiProfiles()
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
                _uiState.update { it.copy(conversations = conversations) }
            }
        }
    }

    private fun observeMessages(conversationId: String) {
        messagesObserverJob?.cancel()
        messagesObserverJob = viewModelScope.launch {
            repository.observeMessages(conversationId).collect { messages ->
                if (!_uiState.value.isStreaming) {
                    _uiState.update { it.copy(messages = messages.toChatMessages()) }
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

        streamingJob = viewModelScope.launch {
            _uiState.update { it.copy(isLoading = true, error = null) }

            try {
                val conversationId = _uiState.value.currentConversationId
                    ?: repository.createConversation().also { newId ->
                        _uiState.update { it.copy(currentConversationId = newId) }
                    }

                val now = System.currentTimeMillis()

                val domainAttachments = if (sendAttachments.isNotEmpty()) {
                    sendAttachments.map { uiAtt ->
                        val dataUrl = AttachmentEncoder.toDataUrl(context, Uri.parse(uiAtt.uri))
                        com.example.aichat.domain.model.Attachment(
                            id = uiAtt.id,
                            mimeType = uiAtt.mimeType,
                            uri = dataUrl
                        )
                    }
                } else emptyList()

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
                    attachments = sendAttachments
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

                try {
                    val stream = if (hasAnyAttachments) {
                        val multimodalMessages = fullHistory.map { msg ->
                            val remoteAttachments = msg.attachments.map { domainAtt ->
                                com.example.aichat.data.remote.Attachment(
                                    url = domainAtt.uri,
                                    mimeType = domainAtt.mimeType
                                )
                            }
                            msg to remoteAttachments
                        }
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
                            }
                            is StreamEvent.ReasoningDelta -> {
                                reasoningBuilder.append(event.text)
                                updateStreamingContent(
                                    aiMessageId,
                                    contentBuilder.toString(),
                                    reasoningBuilder.toString()
                                )
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
                    val aiMessage = Message(
                        id = aiMessageId,
                        conversationId = conversationId,
                        role = Role.ASSISTANT,
                        content = finalContent,
                        timestamp = System.currentTimeMillis(),
                        status = status,
                        reasoningContent = finalReasoning,
                        metadata = if (status == MessageStatus.INTERRUPTED) {
                            MessageMetadata(interruptedReason = "user_cancelled")
                        } else null
                    )
                    withContext(NonCancellable) {
                        repository.appendMessage(aiMessage)
                    }
                }

                _uiState.update { it.copy(isStreaming = false) }
                observeMessages(conversationId)

                if (status == MessageStatus.COMPLETE) {
                    _events.emit(ChatEvent.MessageSent)
                }
            } catch (e: ApiException) {
                handleError(e.message ?: "API request failed", text, sendAttachments)
            } catch (e: NetworkException) {
                handleError(e.message ?: "Network error", text, sendAttachments)
            } catch (e: Exception) {
                handleError(e.message ?: "Unexpected error", text, sendAttachments, retryable = false)
            } finally {
                _uiState.update { it.copy(isLoading = false) }
            }
        }
    }

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
    }

    fun newChat() {
        stopGenerating()
        messagesObserverJob?.cancel()
        _uiState.update {
            it.copy(
                messages = emptyList(),
                currentConversationId = null,
                error = null,
                isLoading = false,
                isStreaming = false,
                pendingAttachments = emptyList()
            )
        }
    }

    fun selectConversation(conversationId: String) {
        stopGenerating()
        _uiState.update {
            it.copy(
                currentConversationId = conversationId,
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

    fun loadConversations() {
        // No-op: conversations are already observed
    }

    fun clearError() {
        _uiState.update { it.copy(error = null) }
    }
}
