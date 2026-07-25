package com.example.aichat.domain.repository

import com.example.aichat.domain.model.Conversation
import com.example.aichat.domain.model.Message
import com.example.aichat.domain.model.MessageMetadata
import kotlinx.coroutines.flow.Flow

interface ChatRepository {

    fun observeConversations(): Flow<List<Conversation>>

    fun observeMessages(conversationId: String): Flow<List<Message>>

    suspend fun getMessages(conversationId: String): List<Message>

    suspend fun createConversation(): String

    suspend fun appendMessage(message: Message)

    suspend fun updateMessage(id: String, content: String)

    /**
     * Lightweight streaming-state write: updates content + reasoning + status
     * in one shot, **without** touching conversation metadata (lastMessage /
     * updatedAt / title). Use this during streaming so we don't spam the
     * conversations table on every token chunk.
     *
     * Finalise the message with [appendMessage] (or a dedicated status update)
     * when the stream completes - this method is for incremental persistence
     * so a crash mid-stream still leaves the partial content on disk.
     */
    suspend fun updateStreamingMessage(
        id: String,
        content: String,
        reasoningContent: String?,
        status: String
    )

    /** Update just the metadata JSON of a message (e.g. interruptedReason). */
    suspend fun updateMessageMetadata(id: String, metadata: MessageMetadata)

    /** Delete a single message by id (used to clean up empty AI placeholders). */
    suspend fun deleteMessage(id: String)

    /** Bump conversation's lastMessage + updatedAt without inserting a message. */
    suspend fun touchConversation(id: String, lastMessage: String, timestamp: Long)

    suspend fun renameConversation(id: String, title: String)

    suspend fun deleteConversation(id: String)

    /** Set or clear AI message feedback: "like" | "dislike" | null. */
    suspend fun setMessageReaction(messageId: String, reaction: String?)
}
