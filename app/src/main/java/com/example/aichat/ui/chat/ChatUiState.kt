package com.example.aichat.ui.chat

import com.example.aichat.domain.model.Conversation
import com.example.aichat.ui.chat.model.Attachment
import com.example.aichat.ui.chat.model.ChatMessage

data class ChatUiState(
    val messages: List<ChatMessage> = emptyList(),
    val isLoading: Boolean = false,
    val isStreaming: Boolean = false,
    val error: String? = null,
    val currentConversationId: String? = null,
    val conversations: List<Conversation> = emptyList(),
    val pendingAttachments: List<Attachment> = emptyList()
)
