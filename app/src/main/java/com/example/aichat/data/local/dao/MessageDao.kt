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
}
