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
 * Streaming lifecycle note:
 *   The actual AI request is executed by [StreamingService] in a dedicated
 *   `:streaming` process so it survives Activity recreation, screen lock and
 *   aggressive OEM background killers. This ViewModel only:
 *     - manages the optimistic UI state
 *     - persists pending attachments to filesDir
 *     - starts/stops the service
 *     - observes Room for messages and conversations
 *   When the service writes the AI reply back to Room, the Flow collected here
 *   updates the UI automatically.
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

    private fun observeMessages(conversationId: String) {
        messagesObserverJob?.cancel()
        messagesObserverJob = viewModelScope.launch {
            repository.observeMessages(conversationId).collect { messages ->
                val hasStreaming = messages.any { it.status == MessageStatus.STREAMING }
                _uiState.update { state ->
                    if (state.isStreaming && hasStreaming) {
                        // The service is still running; let the optimistic
                        // streaming UI keep driving to avoid cursor jumps.
                        state
                    } else {
                        state.copy(
                            messages = messages.toChatMessages(),
                            isStreaming = hasStreaming,
                            isLoading = hasStreaming
                        )
                    }
                }
            }
        }
    }

    /**
     * Send a user message and ask the streaming service to produce an AI reply.
     *
     * Attachments are persisted to filesDir in this call (needs a UI-context
     * ContentResolver), then everything else moves to the :streaming process.
     */
    fun sendMessage(text: String, attachments: List<Attachment> = emptyList()) {
        val trimmed = text.trim()
        val sendAttachments = attachments.ifEmpty { _uiState.value.pendingAttachments }
        val hasContent = trimmed.isNotBlank() || sendAttachments.isNotEmpty()
        if (!hasContent || _uiState.value.isLoading) return

        _uiState.update { it.copy(pendingAttachments = emptyList(), isLoading = true, error = null) }

        viewModelScope.launch {
            try {
                val conversationId = _uiState.value.currentConversationId
                    ?: repository.createConversation().also { newId ->
                        _uiState.update { it.copy(currentConversationId = newId) }
                    }

                // Persist attachments before handing them to the service.
                val persistedAttachments = if (sendAttachments.isNotEmpty()) {
                    sendAttachments.map { uiAtt ->
                        val filePath = AttachmentEncoder.persist(context, Uri.parse(uiAtt.uri))
                        uiAtt.copy(uri = filePath)
                    }
                } else emptyList()

                // Optimistically append the user message to Room so the service
                // sees it when it reads history.
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

                // Optimistic UI.
                val userChatMessage = ChatMessage(
                    id = userMessage.id,
                    role = com.example.aichat.ui.chat.model.Role.USER,
                    content = trimmed,
                    attachments = persistedAttachments
                )
                val streamingAiMessage = ChatMessage(
                    id = UUID.randomUUID().toString(),
                    role = com.example.aichat.ui.chat.model.Role.ASSISTANT,
                    content = "",
                    isStreaming = true
                )
                _uiState.update {
                    it.copy(
                        messages = it.messages + userChatMessage + streamingAiMessage,
                        isStreaming = true
                    )
                }

                StreamingService.start(
                    context = context,
                    conversationId = conversationId,
                    text = trimmed,
                    attachmentsJson = gson.toJson(persistedAttachments)
                )
            } catch (e: Exception) {
                _events.emit(ChatEvent.ShowError(e.message ?: "启动失败", null))
                _uiState.update { it.copy(isLoading = false, isStreaming = false) }
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
     * User-initiated stop. The service receives the stop command and cancels its
     * coroutine; the partial reply has already been persisted incrementally.
     */
    fun stopGenerating() {
        StreamingService.stop(context)
        _uiState.update { it.copy(isLoading = false) }
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
}
