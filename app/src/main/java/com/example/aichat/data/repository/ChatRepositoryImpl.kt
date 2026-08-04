package com.example.aichat.data.repository

import com.example.aichat.data.local.dao.ConversationDao
import com.example.aichat.data.local.dao.MessageDao
import com.example.aichat.data.local.entity.ConversationEntity
import com.example.aichat.data.local.entity.MessageEntity
import com.example.aichat.data.local.mapper.toDomain
import com.example.aichat.data.local.mapper.toEntity
import com.example.aichat.di.IoDispatcher
import com.example.aichat.domain.model.Conversation
import com.example.aichat.domain.model.Message
import com.example.aichat.domain.model.MessageInteractions
import com.example.aichat.domain.model.MessageMetadata
import com.example.aichat.domain.repository.ChatRepository
import com.google.gson.Gson
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.withContext
import java.util.UUID
import javax.inject.Inject

class ChatRepositoryImpl @Inject constructor(
    private val conversationDao: ConversationDao,
    private val messageDao: MessageDao,
    @IoDispatcher private val ioDispatcher: CoroutineDispatcher
) : ChatRepository {

    private val gson = Gson()

    override fun observeConversations(): Flow<List<Conversation>> =
        conversationDao.observeAll().map { list -> list.map { it.toDomain() } }

    override fun observeMessages(conversationId: String): Flow<List<Message>> =
        messageDao.observeByConversation(conversationId).map { list -> list.map { it.toDomain() } }

    override suspend fun getMessages(conversationId: String): List<Message> = withContext(ioDispatcher) {
        messageDao.getByConversation(conversationId).map { it.toDomain() }
    }

    override suspend fun createConversation(): String = withContext(ioDispatcher) {
        val now = System.currentTimeMillis()
        val id = UUID.randomUUID().toString()
        conversationDao.upsert(
            ConversationEntity(
                id = id,
                title = "New Conversation",
                createdAt = now,
                updatedAt = now,
                lastMessage = ""
            )
        )
        id
    }

    override suspend fun appendMessage(message: Message) = withContext(ioDispatcher) {
        messageDao.insert(message.toEntity())
        val existing = conversationDao.getById(message.conversationId)
        val title = if (existing != null && existing.title != "New Conversation" && existing.lastMessage.isNotEmpty()) {
            existing.title
        } else {
            deriveTitle(message.content)
        }
        conversationDao.updateMeta(
            id = message.conversationId,
            title = title,
            lastMessage = message.content.take(100),
            time = message.timestamp
        )
    }

    override suspend fun updateMessage(id: String, content: String) = withContext(ioDispatcher) {
        messageDao.updateContent(id, content)
    }

    override suspend fun updateStreamingMessage(
        id: String,
        content: String,
        reasoningContent: String?,
        status: String
    ) = withContext(ioDispatcher) {
        // Two separate UPDATEs - the DAO doesn't expose a combined setter.
        // Both are O(1) single-row updates by primary key; cheap even at
        // 500ms cadence over a long stream.
        messageDao.updateContent(id, content)
        if (reasoningContent != null) {
            messageDao.updateReasoning(id, reasoningContent)
        }
        messageDao.updateStatus(id, status)
    }

    override suspend fun updateMessageMetadata(id: String, metadata: MessageMetadata) =
        withContext(ioDispatcher) {
            messageDao.updateStatusAndMetadata(
                id = id,
                status = messageDao.getStatusById(id) ?: "COMPLETE",
                metadata = gson.toJson(metadata)
            )
        }

    override suspend fun deleteMessage(id: String) = withContext(ioDispatcher) {
        messageDao.deleteById(id)
    }

    override suspend fun touchConversation(id: String, lastMessage: String, timestamp: Long) =
        withContext(ioDispatcher) {
            val existing = conversationDao.getById(id) ?: return@withContext
            conversationDao.updateMeta(
                id = id,
                title = existing.title,
                lastMessage = lastMessage,
                time = timestamp
            )
        }

    override suspend fun renameConversation(id: String, title: String) = withContext(ioDispatcher) {
        conversationDao.rename(id, title)
    }

    override suspend fun deleteConversation(id: String): Unit = withContext(ioDispatcher) {
        conversationDao.getById(id)?.let { conversationDao.delete(it) }
    }

    override suspend fun setMessageReaction(messageId: String, reaction: String?) =
        withContext(ioDispatcher) {
            messageDao.updateReaction(messageId, reaction)
            messageDao.updateFeedbackSynced(messageId, 0)
        }

    override suspend fun recordInteraction(
        messageId: String,
        type: ChatRepository.InteractionType,
        extraData: Map<String, Any>
    ) = withContext(ioDispatcher) {
        val entity = messageDao.getById(messageId) ?: return@withContext
        val current = MessageInteractions.fromJson(entity.interactionsJson) ?: MessageInteractions()
        val updated = when (type) {
            ChatRepository.InteractionType.SHARE -> current.copy(shared = current.shared + 1)
            ChatRepository.InteractionType.RETRY -> current.copy(retryCount = current.retryCount + 1)
            ChatRepository.InteractionType.TTS_PLAYBACK -> {
                val duration = (extraData["duration_ms"] as? Number)?.toLong() ?: 0L
                current.copy(
                    ttsCount = current.ttsCount + 1,
                    ttsTotalDurationMs = current.ttsTotalDurationMs + duration
                )
            }
        }
        messageDao.updateInteractions(messageId, updated.toJson())
        messageDao.updateFeedbackSynced(messageId, 0)
    }

    override suspend fun getUnsyncedFeedback(): List<Message> = withContext(ioDispatcher) {
        messageDao.getUnsyncedFeedback().map { it.toDomain() }
    }

    override suspend fun markFeedbackSynced(messageIds: List<String>) = withContext(ioDispatcher) {
        messageIds.forEach { messageDao.updateFeedbackSynced(it, 1) }
    }

    override suspend fun getMessagesSince(
        conversationId: String,
        sinceTimestamp: Long
    ): List<Message> = withContext(ioDispatcher) {
        messageDao.getMessagesSince(conversationId, sinceTimestamp).map { it.toDomain() }
    }

    override suspend fun markDanglingStreamingInterrupted(reason: String) =
        withContext(ioDispatcher) {
            val metadataJson = gson.toJson(MessageMetadata(interruptedReason = reason))
            messageDao.markStreamingAsInterrupted(metadataJson)
        }

    override suspend fun markConversationStreamingInterrupted(
        conversationId: String,
        reason: String
    ) = withContext(ioDispatcher) {
        val metadataJson = gson.toJson(MessageMetadata(interruptedReason = reason))
        messageDao.markStreamingAsInterruptedForConversation(conversationId, metadataJson)
    }

    override fun observeStreamingConversationIds(): Flow<Set<String>> =
        messageDao.observeStreamingConversationIds().map { it.toSet() }

    override suspend fun getMessageStatus(messageId: String): String? =
        withContext(ioDispatcher) { messageDao.getStatusById(messageId) }

    private fun deriveTitle(content: String): String {
        val trimmed = content.trim().take(50)
        return if (trimmed.isEmpty()) "New Conversation" else trimmed
    }
}
