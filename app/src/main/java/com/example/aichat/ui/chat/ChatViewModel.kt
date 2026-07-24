package com.example.aichat.ui.chat

import android.content.Context
import android.net.Uri
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.example.aichat.data.remote.ApiException
import com.example.aichat.data.remote.AttachmentEncoder
import com.example.aichat.data.remote.ChatRemoteDataSource
import com.example.aichat.data.remote.NetworkException
import com.example.aichat.domain.model.Message
import com.example.aichat.domain.model.Role
import com.example.aichat.domain.repository.ChatRepository
import com.example.aichat.ui.chat.model.Attachment
import com.example.aichat.ui.chat.model.ChatMessage
import com.example.aichat.ui.chat.model.toChatMessages
import dagger.hilt.android.lifecycle.HiltViewModel
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import java.util.UUID

@HiltViewModel
class ChatViewModel @Inject constructor(
    private val repository: ChatRepository,
    private val remoteDataSource: ChatRemoteDataSource,
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
     * Attachment encoding flow:
     * 1. UI attachments have content:// URIs (from PickVisualMedia)
     * 2. AttachmentEncoder.toDataUrl() converts them to base64 data URLs
     * 3. Domain Message is created with encoded attachments and persisted to Room
     * 4. streamChatMultimodal sends text + image_url parts to the API
     */
    fun sendMessage(text: String, attachments: List<Attachment> = emptyList()) {
        val trimmed = text.trim()
        val sendAttachments = attachments.ifEmpty { _uiState.value.pendingAttachments }
        val hasContent = trimmed.isNotBlank() || sendAttachments.isNotEmpty()
        if (!hasContent || _uiState.value.isLoading) return

        // Clear pending attachments
        _uiState.update { it.copy(pendingAttachments = emptyList()) }

        streamingJob = viewModelScope.launch {
            _uiState.update { it.copy(isLoading = true, error = null) }

            try {
                val conversationId = _uiState.value.currentConversationId
                    ?: repository.createConversation().also { newId ->
                        _uiState.update { it.copy(currentConversationId = newId) }
                    }

                val now = System.currentTimeMillis()

                // Encode attachments to base64 data URLs for persistence and API
                val domainAttachments = if (sendAttachments.isNotEmpty()) {
                    sendAttachments.map { uiAtt ->
                        val dataUrl = AttachmentEncoder.toDataUrl(context, Uri.parse(uiAtt.uri))
                        com.example.aichat.domain.model.Attachment(
                            id = uiAtt.id,
                            mimeType = uiAtt.mimeType,
                            uri = dataUrl
                        )
                    }
                } else {
                    emptyList()
                }

                // Persist user message with encoded attachments
                val userMessage = Message(
                    id = UUID.randomUUID().toString(),
                    conversationId = conversationId,
                    role = Role.USER,
                    content = trimmed,
                    timestamp = now,
                    attachments = domainAttachments
                )
                repository.appendMessage(userMessage)

                // Show user message in UI (use original content:// URIs for display)
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

                // Build context from persisted messages for API call
                val fullHistory = repository.getMessages(conversationId)

                // Use multimodal endpoint if any message has attachments
                val hasAnyAttachments = fullHistory.any { it.attachments.isNotEmpty() }

                val contentBuilder = StringBuilder()
                val streamCompleted = try {
                    if (hasAnyAttachments) {
                        // Build pairs of (Message, List<RemoteAttachment>) for multimodal API
                        val multimodalMessages = fullHistory.map { msg ->
                            val remoteAttachments = msg.attachments.map { domainAtt ->
                                com.example.aichat.data.remote.Attachment(
                                    url = domainAtt.uri,
                                    mimeType = domainAtt.mimeType
                                )
                            }
                            msg to remoteAttachments
                        }
                        remoteDataSource.streamChatMultimodal(multimodalMessages).collectLatest { token ->
                            contentBuilder.append(token)
                            updateStreamingContent(aiMessageId, contentBuilder.toString())
                        }
                    } else {
                        remoteDataSource.streamChat(fullHistory).collectLatest { token ->
                            contentBuilder.append(token)
                            updateStreamingContent(aiMessageId, contentBuilder.toString())
                        }
                    }
                    true
                } catch (e: kotlinx.coroutines.CancellationException) {
                    false
                }

                // Persist AI message
                val finalContent = contentBuilder.toString()
                if (finalContent.isNotBlank()) {
                    val aiMessage = Message(
                        id = aiMessageId,
                        conversationId = conversationId,
                        role = Role.ASSISTANT,
                        content = finalContent,
                        timestamp = System.currentTimeMillis()
                    )
                    kotlinx.coroutines.withContext(kotlinx.coroutines.NonCancellable) {
                        repository.appendMessage(aiMessage)
                    }
                }

                _uiState.update { it.copy(isStreaming = false) }
                observeMessages(conversationId)

                if (streamCompleted) {
                    _events.emit(ChatEvent.MessageSent)
                }

            } catch (e: ApiException) {
                val errorMsg = e.message ?: "API request failed"
                _uiState.update { it.copy(error = errorMsg, isStreaming = false) }
                _events.emit(ChatEvent.ShowError(errorMsg) { sendMessage(text, sendAttachments) })
                removeStreamingPlaceholder()
            } catch (e: NetworkException) {
                val errorMsg = e.message ?: "Network error"
                _uiState.update { it.copy(error = errorMsg, isStreaming = false) }
                _events.emit(ChatEvent.ShowError(errorMsg) { sendMessage(text, sendAttachments) })
                removeStreamingPlaceholder()
            } catch (e: Exception) {
                val errorMsg = e.message ?: "Unexpected error"
                _uiState.update { it.copy(error = errorMsg, isStreaming = false) }
                _events.emit(ChatEvent.ShowError(errorMsg, null))
                removeStreamingPlaceholder()
            } finally {
                _uiState.update { it.copy(isLoading = false) }
            }
        }
    }

    /**
     * Update the streaming AI message content in the UI state.
     */
    private fun updateStreamingContent(aiMessageId: String, content: String) {
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
