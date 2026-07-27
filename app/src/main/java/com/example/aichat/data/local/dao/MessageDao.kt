package com.example.aichat.data.local.dao

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import com.example.aichat.data.local.entity.MessageEntity
import kotlinx.coroutines.flow.Flow

@Dao
interface MessageDao {

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insert(message: MessageEntity)

    @Query("SELECT * FROM messages WHERE conversationId = :id ORDER BY timestamp ASC")
    fun observeByConversation(id: String): Flow<List<MessageEntity>>

    @Query("SELECT COUNT(*) FROM messages WHERE conversationId = :id")
    suspend fun count(id: String): Int

    @Query("SELECT * FROM messages WHERE conversationId = :id ORDER BY timestamp ASC")
    suspend fun getByConversation(id: String): List<MessageEntity>

    @Query("UPDATE messages SET content = :content, updatedAt = :updatedAt WHERE id = :id")
    suspend fun updateContent(id: String, content: String, updatedAt: Long = System.currentTimeMillis())

    @Query("UPDATE messages SET status = :status, updatedAt = :updatedAt WHERE id = :id")
    suspend fun updateStatus(id: String, status: String, updatedAt: Long = System.currentTimeMillis())

    @Query("UPDATE messages SET reasoningContent = :content, updatedAt = :updatedAt WHERE id = :id")
    suspend fun updateReasoning(id: String, content: String, updatedAt: Long = System.currentTimeMillis())

    @Query("UPDATE messages SET status = :status, metadataJson = :metadata WHERE id = :id")
    suspend fun updateStatusAndMetadata(id: String, status: String, metadata: String?)

    @Query("UPDATE messages SET reaction = :reaction WHERE id = :id")
    suspend fun updateReaction(id: String, reaction: String?)

    /**
     * Bulk-flip every row stuck in STREAMING to INTERRUPTED, attaching the
     * supplied metadataJson (e.g. {"interruptedReason":"process_killed"}).
     *
     * **v4.2.6**: callers MUST scope this to rows whose `updatedAt` is
     * older than [olderThan] — a fresh STREAMING row is almost certainly
     * being written right now by the :streaming process and must not be
     * touched. The previous "sweep everything" version caused the
     * recurring "AI bubble disappears after lock screen" regression.
     */
    @Query(
        "UPDATE messages SET status = 'INTERRUPTED', metadataJson = :metadataJson, " +
            "updatedAt = :nowMs " +
            "WHERE status = 'STREAMING' AND updatedAt < :olderThan"
    )
    suspend fun markStreamingAsInterrupted(metadataJson: String, olderThan: Long, nowMs: Long)

    /**
     * Same as [markStreamingAsInterrupted] but scoped to a single conversation.
     * Used at the start of [StreamAiReplyUseCase.invoke] so that a previous
     * in-flight placeholder (whose :streaming process was killed before it
     * could finalise) does not collide with the new placeholder about to be
     * inserted.
     */
    @Query(
        "UPDATE messages SET status = 'INTERRUPTED', metadataJson = :metadataJson " +
            "WHERE conversationId = :conversationId AND status = 'STREAMING'"
    )
    suspend fun markStreamingAsInterruptedForConversation(
        conversationId: String,
        metadataJson: String
    )

    @Query("SELECT status FROM messages WHERE id = :id")
    suspend fun getStatusById(id: String): String?

    /**
     * Emits the set of conversationIds that currently have at least one message
     * in STREAMING state. Drives the drawer's green-dot indicator and the
     * concurrent-stream cap in the ViewModel.
     */
    @Query("SELECT DISTINCT conversationId FROM messages WHERE status = 'STREAMING'")
    fun observeStreamingConversationIds(): Flow<List<String>>

    @Query("DELETE FROM messages WHERE id = :id")
    suspend fun deleteById(id: String)
}
