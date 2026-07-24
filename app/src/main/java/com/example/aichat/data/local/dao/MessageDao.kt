package com.example.aichat.data.local.dao

import androidx.room.Insert
import androidx.room.Query
import com.example.aichat.data.local.entity.MessageEntity
import kotlinx.coroutines.flow.Flow

interface MessageDao {

    @Insert
    suspend fun insert(message: MessageEntity)

    @Query("SELECT * FROM messages WHERE conversationId = :id ORDER BY timestamp ASC")
    fun observeByConversation(id: String): Flow<List<MessageEntity>>

    @Query("SELECT COUNT(*) FROM messages WHERE conversationId = :id")
    suspend fun count(id: String): Int

    @Query("SELECT * FROM messages WHERE conversationId = :id ORDER BY timestamp ASC")
    suspend fun getByConversation(id: String): List<MessageEntity>

    @Query("UPDATE messages SET content = :content WHERE id = :id")
    suspend fun updateContent(id: String, content: String)
}
