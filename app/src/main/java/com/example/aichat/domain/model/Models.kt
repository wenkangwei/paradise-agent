package com.example.aichat.domain.model

data class Conversation(
    val id: String,
    val title: String,
    val createdAt: Long,
    val updatedAt: Long,
    val lastMessage: String = ""
)

data class Attachment(
    val id: String,
    val mimeType: String,
    val uri: String
)

data class Message(
    val id: String,
    val conversationId: String,
    val role: Role,
    val content: String,
    val timestamp: Long,
    val attachments: List<Attachment> = emptyList()
)

enum class Role { USER, ASSISTANT, SYSTEM }
