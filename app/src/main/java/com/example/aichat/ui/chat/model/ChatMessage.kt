package com.example.aichat.ui.chat.model

data class ChatMessage(
    val id: String,
    val role: Role,
    val content: String,
    val isStreaming: Boolean = false,
    val attachments: List<Attachment> = emptyList()
)

data class Attachment(
    val id: String,
    val uri: String,
    val mimeType: String,
    val displayName: String = ""
)

enum class Role { USER, ASSISTANT, SYSTEM }
