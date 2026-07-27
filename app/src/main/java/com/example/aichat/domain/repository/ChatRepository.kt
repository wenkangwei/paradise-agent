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

    /**
     * Mark every message still in STREAMING state as INTERRUPTED, attaching
     * the supplied reason to its metadata. Call once on app start to clean
     * up rows orphaned by a crashed/killed `:streaming` process.
     */
    suspend fun markDanglingStreamingInterrupted(reason: String)

    /**
     * Same as [markDanglingStreamingInterrupted] but scoped to a single
     * conversation. Used before inserting a new AI placeholder so a stale
     * STREAMING row from a killed previous run does not collide with the
     * new one and confuse the UI observer.
     */
    suspend fun markConversationStreamingInterrupted(
        conversationId: String,
        reason: String
    )

    /**
     * Emits the set of conversationIds that currently have at least one
     * message in STREAMING state. Drives the drawer's green-dot indicator
     * and the ViewModel's concurrent-stream cap.
     */
    fun observeStreamingConversationIds(): Flow<Set<String>>

    /** Single-row status read — used by the streaming UseCase to detect
     *  external sweeps (e.g. app restart dangling cleanup) and bail out
     *  instead of overwriting INTERRUPTED with STREAMING again. */
    suspend fun getMessageStatus(messageId: String): String?
}
