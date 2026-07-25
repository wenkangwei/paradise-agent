package com.example.aichat.ui.chat.model

import com.example.aichat.domain.model.MessageMetadata
import com.example.aichat.domain.model.MessageStatus

data class ChatMessage(
    val id: String,
    val role: Role,
    val content: String,
    val isStreaming: Boolean = false,
    val attachments: List<Attachment> = emptyList(),
    val reasoningContent: String? = null,
    val metadata: MessageMetadata? = null,
    val reaction: String? = null,
    /** Room status, used to decide when to stop the streaming spinner. */
    val status: MessageStatus? = null
)

data class Attachment(
    val id: String,
    val uri: String,
    val mimeType: String,
    val displayName: String = ""
)

enum class Role { USER, ASSISTANT, SYSTEM }
