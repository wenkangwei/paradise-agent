package com.example.aichat.data.repository

import com.example.aichat.data.local.dao.ConversationDao
import com.example.aichat.data.local.dao.MessageDao
import com.example.aichat.data.local.entity.ConversationEntity
import com.example.aichat.data.local.mapper.toDomain
import com.example.aichat.data.local.mapper.toEntity
import com.example.aichat.di.IoDispatcher
import com.example.aichat.domain.model.Conversation
import com.example.aichat.domain.model.Message
import com.example.aichat.domain.repository.ChatRepository
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

    override suspend fun renameConversation(id: String, title: String) = withContext(ioDispatcher) {
        conversationDao.rename(id, title)
    }

    override suspend fun deleteConversation(id: String): Unit = withContext(ioDispatcher) {
        conversationDao.getById(id)?.let { conversationDao.delete(it) }
    }

    override suspend fun setMessageReaction(messageId: String, reaction: String?) =
        withContext(ioDispatcher) {
            messageDao.updateReaction(messageId, reaction)
        }

    private fun deriveTitle(content: String): String {
        val trimmed = content.trim().take(50)
        return if (trimmed.isEmpty()) "New Conversation" else trimmed
    }
}
