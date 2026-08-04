package com.example.aichat.data.local.entity

import androidx.room.Entity
import androidx.room.ForeignKey
import androidx.room.Index
import androidx.room.PrimaryKey

@Entity(
    tableName = "messages",
    foreignKeys = [
        ForeignKey(
            entity = ConversationEntity::class,
            parentColumns = ["id"],
            childColumns = ["conversationId"],
            onDelete = ForeignKey.CASCADE
        )
    ],
    indices = [Index("conversationId")]
)
data class MessageEntity(
    @PrimaryKey val id: String,
    val conversationId: String,
    val role: String,
    val content: String,
    val timestamp: Long,
    val attachmentsJson: String = "[]",
    val status: String = "COMPLETE",
    val reasoningContent: String? = null,
    val metadataJson: String? = null,
    /**
     * User feedback on AI messages: "like" | "dislike" | null.
     * Null on user messages. Added in v6 (MIGRATION_5_6).
     */
    val reaction: String? = null,
    /**
     * JSON-encoded interaction metrics: {shared, retry_count, tts_count, tts_duration_ms}.
     * Accumulated per-message. Added in v10 (MIGRATION_9_10).
     */
    val interactionsJson: String? = null,
    /**
     * Whether this message's feedback data has been synced to the server.
     * 0 = not synced, 1 = synced. Added in v10 (MIGRATION_9_10).
     */
    val feedbackSynced: Int = 0
)
