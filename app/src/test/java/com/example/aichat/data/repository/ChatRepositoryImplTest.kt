package com.example.aichat.data.repository

import com.example.aichat.data.local.dao.ConversationDao
import com.example.aichat.data.local.dao.MessageDao
import com.example.aichat.data.local.entity.ConversationEntity
import com.example.aichat.data.local.entity.MessageEntity
import com.example.aichat.domain.model.Message
import com.example.aichat.domain.model.Role
import io.mockk.coEvery
import io.mockk.coVerify
import io.mockk.coVerifyOrder
import io.mockk.every
import io.mockk.mockk
import io.mockk.slot
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Before
import org.junit.Test

class ChatRepositoryImplTest {

    private lateinit var conversationDao: ConversationDao
    private lateinit var messageDao: MessageDao
    private lateinit var repository: ChatRepositoryImpl

    @Before
    fun setup() {
        conversationDao = mockk(relaxed = true)
        messageDao = mockk(relaxed = true)
        repository = ChatRepositoryImpl(
            conversationDao = conversationDao,
            messageDao = messageDao,
            ioDispatcher = Dispatchers.Unconfined
        )
    }

    // ── observeMessages ──

    @Test
    fun observeMessages_forwardsFlowFromDao() = runTest {
        val entities = listOf(
            MessageEntity("m1", "c1", "USER", "Hello", 1000L),
            MessageEntity("m2", "c1", "ASSISTANT", "Hi there", 2000L)
        )
        every { messageDao.observeByConversation("c1") } returns flowOf(entities)

        val result = repository.observeMessages("c1")

        result.collect { messages ->
            assertEquals(2, messages.size)
            assertEquals("Hello", messages[0].content)
            assertEquals(Role.USER, messages[0].role)
            assertEquals("Hi there", messages[1].content)
            assertEquals(Role.ASSISTANT, messages[1].role)
        }
    }

    // ── createConversation ──

    @Test
    fun createConversation_insertsEntityAndReturnsId() = runTest {
        val capturedEntity = slot<ConversationEntity>()
        coEvery { conversationDao.upsert(capture(capturedEntity)) } returns Unit

        val id = repository.createConversation()

        assertNotNull(id)
        assertEquals(id, capturedEntity.captured.id)
        assertEquals("New Conversation", capturedEntity.captured.title)
        assertEquals("", capturedEntity.captured.lastMessage)
    }

    // ── appendMessage ──

    @Test
    fun appendMessage_insertsMessageAndUpdatesConversationMeta() = runTest {
        val existingConv = ConversationEntity(
            id = "c1",
            title = "Existing Title",
            createdAt = 1000L,
            updatedAt = 2000L,
            lastMessage = "old preview"
        )
        coEvery { conversationDao.getById("c1") } returns existingConv

        val message = Message(
            id = "m1",
            conversationId = "c1",
            role = Role.USER,
            content = "New message content",
            timestamp = 5000L
        )

        repository.appendMessage(message)

        val capturedEntity = slot<MessageEntity>()
        coVerify { messageDao.insert(capture(capturedEntity)) }
        assertEquals("m1", capturedEntity.captured.id)
        assertEquals("c1", capturedEntity.captured.conversationId)

        coVerify {
            conversationDao.updateMeta(
                id = "c1",
                title = "Existing Title",
                lastMessage = "New message content",
                time = 5000L
            )
        }
    }

    @Test
    fun appendMessage_firstMessage_derivesTitleFromContent() = runTest {
        coEvery { conversationDao.getById("c1") } returns ConversationEntity(
            id = "c1",
            title = "New Conversation",
            createdAt = 1000L,
            updatedAt = 1000L,
            lastMessage = ""
        )

        val message = Message(
            id = "m1",
            conversationId = "c1",
            role = Role.USER,
            content = "What is Kotlin?",
            timestamp = 3000L
        )

        repository.appendMessage(message)

        coVerify {
            conversationDao.updateMeta(
                id = "c1",
                title = "What is Kotlin?",
                lastMessage = "What is Kotlin?",
                time = 3000L
            )
        }
    }

    @Test
    fun appendMessage_longContent_truncatesLastMessage() = runTest {
        coEvery { conversationDao.getById("c1") } returns ConversationEntity(
            id = "c1",
            title = "New Conversation",
            createdAt = 0L,
            updatedAt = 0L
        )

        val longContent = "A".repeat(200)
        val message = Message(
            id = "m1",
            conversationId = "c1",
            role = Role.USER,
            content = longContent,
            timestamp = 1000L
        )

        repository.appendMessage(message)

        coVerify {
            conversationDao.updateMeta(
                id = "c1",
                title = any(),
                lastMessage = "A".repeat(100),
                time = 1000L
            )
        }
    }

    @Test
    fun appendMessage_callsInsertBeforeUpdateMeta() = runTest {
        coEvery { conversationDao.getById("c1") } returns null

        val message = Message(
            id = "m1",
            conversationId = "c1",
            role = Role.USER,
            content = "Hello",
            timestamp = 1000L
        )

        repository.appendMessage(message)

        coVerifyOrder {
            messageDao.insert(any())
            conversationDao.getById("c1")
            conversationDao.updateMeta(any(), any(), any(), any())
        }
    }

    // ── deleteConversation ──

    @Test
    fun deleteConversation_whenExists_deletesEntity() = runTest {
        val conv = ConversationEntity("c1", "Title", 1000L, 2000L, "preview")
        coEvery { conversationDao.getById("c1") } returns conv

        repository.deleteConversation("c1")

        coVerify { conversationDao.delete(conv) }
    }

    @Test
    fun deleteConversation_whenNotFound_doesNotCallDelete() = runTest {
        coEvery { conversationDao.getById("c1") } returns null

        repository.deleteConversation("c1")

        coVerify(exactly = 0) { conversationDao.delete(any()) }
    }

    // ── renameConversation ──

    @Test
    fun renameConversation_callsDaoRename() = runTest {
        repository.renameConversation("c1", "New Name")

        coVerify { conversationDao.rename("c1", "New Name") }
    }

    // ── updateMessage ──

    @Test
    fun updateMessage_callsDaoUpdateContent() = runTest {
        repository.updateMessage("m1", "updated content")

        coVerify { messageDao.updateContent("m1", "updated content") }
    }
}
