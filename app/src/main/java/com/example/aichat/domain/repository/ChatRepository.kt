package com.example.aichat.domain.repository

import com.example.aichat.domain.model.Conversation
import com.example.aichat.domain.model.Message
import kotlinx.coroutines.flow.Flow

interface ChatRepository {

    fun observeConversations(): Flow<List<Conversation>>

    fun observeMessages(conversationId: String): Flow<List<Message>>

    suspend fun getMessages(conversationId: String): List<Message>

    suspend fun createConversation(): String

    suspend fun appendMessage(message: Message)

    suspend fun updateMessage(id: String, content: String)

    suspend fun renameConversation(id: String, title: String)

    suspend fun deleteConversation(id: String)

    /** Set or clear AI message feedback: "like" | "dislike" | null. */
    suspend fun setMessageReaction(messageId: String, reaction: String?)
}
