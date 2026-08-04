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
    val reaction: String? = null,
    val interactions: MessageInteractions? = null
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

/**
 * Accumulated interaction metrics for a message, used for training feedback.
 * Stored as JSON in MessageEntity.interactionsJson.
 */
data class MessageInteractions(
    val shared: Int = 0,
    val retryCount: Int = 0,
    val ttsCount: Int = 0,
    val ttsTotalDurationMs: Long = 0L
) {
    fun toJson(): String {
        return """{"shared":$shared,"retry_count":$retryCount,"tts_count":$ttsCount,"tts_total_duration_ms":$ttsTotalDurationMs}"""
    }

    companion object {
        fun fromJson(json: String?): MessageInteractions? {
            if (json.isNullOrBlank()) return null
            return try {
                val regex = Regex("\"(\\w+)\":(\\d+)")
                val map = regex.findAll(json).associate { it.groupValues[1] to it.groupValues[2].toLong() }
                MessageInteractions(
                    shared = (map["shared"] ?: 0).toInt(),
                    retryCount = (map["retry_count"] ?: 0).toInt(),
                    ttsCount = (map["tts_count"] ?: 0).toInt(),
                    ttsTotalDurationMs = map["tts_total_duration_ms"] ?: 0L
                )
            } catch (_: Exception) {
                null
            }
        }
    }
}
