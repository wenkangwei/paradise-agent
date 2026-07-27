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
     * Wall-clock millis of the last write to this row. Updated by
     * `StreamAiReplyUseCase.maybePersistStreaming` every persist tick
     * (~150ms while streaming). The main process's init-time watchdog
     * uses this to distinguish a TRULY orphaned STREAMING row (no
     * writer for >2 min) from an actively-streaming one — preventing
     * the regression where the watchdog marked active streams as
     * interrupted after a lock-screen / process-restart cycle.
     *
     * Added in v10 (MIGRATION_9_10). Defaults to `timestamp` for
     * legacy rows (they're all COMPLETE/INTERRUPTED anyway).
     */
    val updatedAt: Long = timestamp
)
