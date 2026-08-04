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

    @Query("UPDATE messages SET content = :content WHERE id = :id")
    suspend fun updateContent(id: String, content: String)

    @Query("UPDATE messages SET status = :status WHERE id = :id")
    suspend fun updateStatus(id: String, status: String)

    @Query("UPDATE messages SET reasoningContent = :content WHERE id = :id")
    suspend fun updateReasoning(id: String, content: String)

    @Query("UPDATE messages SET status = :status, metadataJson = :metadata WHERE id = :id")
    suspend fun updateStatusAndMetadata(id: String, status: String, metadata: String?)

    @Query("UPDATE messages SET reaction = :reaction WHERE id = :id")
    suspend fun updateReaction(id: String, reaction: String?)

    /**
     * Bulk-flip every row stuck in STREAMING to INTERRUPTED, attaching the
     * supplied metadataJson (e.g. {"interruptedReason":"process_killed"}).
     * Used on app start to recover from a `:streaming` process that died
     * mid-stream — otherwise the UI would spin forever on the stale row.
     */
    @Query(
        "UPDATE messages SET status = 'INTERRUPTED', metadataJson = :metadataJson " +
            "WHERE status = 'STREAMING'"
    )
    suspend fun markStreamingAsInterrupted(metadataJson: String)

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

    @Query("SELECT * FROM messages WHERE id = :id")
    suspend fun getById(id: String): MessageEntity?

    /**
     * Emits the set of conversationIds that currently have at least one message
     * in STREAMING state. Drives the drawer's green-dot indicator and the
     * concurrent-stream cap in the ViewModel.
     */
    @Query("SELECT DISTINCT conversationId FROM messages WHERE status = 'STREAMING'")
    fun observeStreamingConversationIds(): Flow<List<String>>

    @Query("DELETE FROM messages WHERE id = :id")
    suspend fun deleteById(id: String)

    // ── Feedback collection queries (v10) ──────────────────────────

    @Query("UPDATE messages SET interactionsJson = :json WHERE id = :id")
    suspend fun updateInteractions(id: String, json: String)

    @Query("UPDATE messages SET feedbackSynced = :synced WHERE id = :id")
    suspend fun updateFeedbackSynced(id: String, synced: Int)

    /**
     * Returns all AI messages with unsynced feedback (reaction or interactions).
     * Used by [FeedbackSyncUseCase] to batch-upload to server.
     */
    @Query(
        "SELECT * FROM messages WHERE feedbackSynced = 0 " +
            "AND (reaction IS NOT NULL OR interactionsJson IS NOT NULL) " +
            "ORDER BY timestamp ASC"
    )
    suspend fun getUnsyncedFeedback(): List<MessageEntity>

    @Query("SELECT * FROM messages WHERE conversationId = :convId AND timestamp > :since ORDER BY timestamp ASC")
    suspend fun getMessagesSince(convId: String, since: Long): List<MessageEntity>
}
