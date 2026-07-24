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
    val attachments: List<Attachment> = emptyList(),
    val status: MessageStatus = MessageStatus.COMPLETE,
    val reasoningContent: String? = null,
    val metadata: MessageMetadata? = null,
    val reaction: String? = null
)

enum class Role { USER, ASSISTANT, SYSTEM }

/**
 * Lifecycle status of a message.
 * - COMPLETE: AI finished normally
 * - STREAMING: in-progress (only used in UI state, not persisted)
 * - INTERRUPTED: user pressed stop with partial content saved
 * - FAILED: API/network error
 */
enum class MessageStatus {
    COMPLETE,
    STREAMING,
    INTERRUPTED,
    FAILED
}

data class MessageMetadata(
    val durationMs: Long? = null,
    val tokenCount: Int? = null,
    val interruptedReason: String? = null,
    val errorCategory: String? = null,
    val searchResults: List<SearchResult> = emptyList()
) {
    /**
     * One retrieved web/document hit surfaced by RAG.
     * `url` may be null for local-doc-only backends.
     */
    data class SearchResult(
        val title: String,
        val snippet: String,
        val url: String?,
        val score: Float = 0f
    )
}
