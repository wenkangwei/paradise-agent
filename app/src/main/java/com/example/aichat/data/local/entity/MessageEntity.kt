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
    val reaction: String? = null
)
