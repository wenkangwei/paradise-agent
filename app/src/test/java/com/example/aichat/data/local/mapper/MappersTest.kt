package com.example.aichat.data.local.mapper

import com.example.aichat.data.local.entity.ConversationEntity
import com.example.aichat.data.local.entity.MessageEntity
import com.example.aichat.domain.model.Attachment
import com.example.aichat.domain.model.Conversation
import com.example.aichat.domain.model.Message
import com.example.aichat.domain.model.Role
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class MappersTest {

    // ── ConversationEntity ↔ Conversation ──

    @Test
    fun conversationEntity_toDomain_mapsAllFields() {
        val entity = ConversationEntity(
            id = "conv-1",
            title = "Test Conversation",
            createdAt = 1000L,
            updatedAt = 2000L,
            lastMessage = "Hello"
        )

        val domain = entity.toDomain()

        assertEquals("conv-1", domain.id)
        assertEquals("Test Conversation", domain.title)
        assertEquals(1000L, domain.createdAt)
        assertEquals(2000L, domain.updatedAt)
        assertEquals("Hello", domain.lastMessage)
    }

    @Test
    fun conversation_toEntity_mapsAllFields() {
        val domain = Conversation(
            id = "conv-2",
            title = "Domain Conversation",
            createdAt = 3000L,
            updatedAt = 4000L,
            lastMessage = "World"
        )

        val entity = domain.toEntity()

        assertEquals("conv-2", entity.id)
        assertEquals("Domain Conversation", entity.title)
        assertEquals(3000L, entity.createdAt)
        assertEquals(4000L, entity.updatedAt)
        assertEquals("World", entity.lastMessage)
    }

    @Test
    fun conversation_roundTrip_preservesData() {
        val original = Conversation(
            id = "conv-rt",
            title = "Round Trip",
            createdAt = 5000L,
            updatedAt = 6000L,
            lastMessage = "Ping"
        )

        val result = original.toEntity().toDomain()

        assertEquals(original, result)
    }

    // ── MessageEntity ↔ Message ──

    @Test
    fun messageEntity_toDomain_mapsAllFields() {
        val entity = MessageEntity(
            id = "msg-1",
            conversationId = "conv-1",
            role = "USER",
            content = "Hello World",
            timestamp = 1000L,
            attachmentsJson = """[{"id":"att-1","mimeType":"image/png","uri":"content://img/1"}]"""
        )

        val domain = entity.toDomain()

        assertEquals("msg-1", domain.id)
        assertEquals("conv-1", domain.conversationId)
        assertEquals(Role.USER, domain.role)
        assertEquals("Hello World", domain.content)
        assertEquals(1000L, domain.timestamp)
        assertEquals(1, domain.attachments.size)
        assertEquals("att-1", domain.attachments[0].id)
        assertEquals("image/png", domain.attachments[0].mimeType)
        assertEquals("content://img/1", domain.attachments[0].uri)
    }

    @Test
    fun messageEntity_toDomain_invalidRole_defaultsToSystem() {
        val entity = MessageEntity(
            id = "msg-x",
            conversationId = "conv-1",
            role = "INVALID_ROLE",
            content = "test",
            timestamp = 0L
        )

        val domain = entity.toDomain()

        assertEquals(Role.SYSTEM, domain.role)
    }

    @Test
    fun messageEntity_toDomain_emptyAttachmentsJson_returnsEmptyList() {
        val entity = MessageEntity(
            id = "msg-2",
            conversationId = "conv-1",
            role = "ASSISTANT",
            content = "No attachments",
            timestamp = 2000L,
            attachmentsJson = "[]"
        )

        val domain = entity.toDomain()

        assertTrue(domain.attachments.isEmpty())
    }

    @Test
    fun messageEntity_toDomain_blankAttachmentsJson_returnsEmptyList() {
        val entity = MessageEntity(
            id = "msg-3",
            conversationId = "conv-1",
            role = "ASSISTANT",
            content = "Blank JSON",
            timestamp = 3000L,
            attachmentsJson = ""
        )

        val domain = entity.toDomain()

        assertTrue(domain.attachments.isEmpty())
    }

    @Test
    fun message_toEntity_mapsAllFields() {
        val domain = Message(
            id = "msg-4",
            conversationId = "conv-1",
            role = Role.ASSISTANT,
            content = "Reply",
            timestamp = 4000L,
            attachments = listOf(
                Attachment("att-2", "image/jpeg", "content://img/2")
            )
        )

        val entity = domain.toEntity()

        assertEquals("msg-4", entity.id)
        assertEquals("conv-1", entity.conversationId)
        assertEquals("ASSISTANT", entity.role)
        assertEquals("Reply", entity.content)
        assertEquals(4000L, entity.timestamp)
        assertTrue(entity.attachmentsJson.contains("att-2"))
        assertTrue(entity.attachmentsJson.contains("image/jpeg"))
        assertTrue(entity.attachmentsJson.contains("content://img/2"))
    }

    @Test
    fun message_toEntity_emptyAttachments_producesEmptyJsonArray() {
        val domain = Message(
            id = "msg-5",
            conversationId = "conv-1",
            role = Role.USER,
            content = "No files",
            timestamp = 5000L
        )

        val entity = domain.toEntity()

        assertEquals("[]", entity.attachmentsJson)
    }

    @Test
    fun message_roundTrip_preservesDataWithAttachments() {
        val original = Message(
            id = "msg-rt",
            conversationId = "conv-rt",
            role = Role.USER,
            content = "Round trip with attachments",
            timestamp = 9000L,
            attachments = listOf(
                Attachment("att-a", "image/png", "content://photo/a"),
                Attachment("att-b", "image/webp", "content://photo/b")
            )
        )

        val result = original.toEntity().toDomain()

        assertEquals(original.id, result.id)
        assertEquals(original.conversationId, result.conversationId)
        assertEquals(original.role, result.role)
        assertEquals(original.content, result.content)
        assertEquals(original.timestamp, result.timestamp)
        assertEquals(original.attachments.size, result.attachments.size)
        assertEquals(original.attachments[0], result.attachments[0])
        assertEquals(original.attachments[1], result.attachments[1])
    }

    @Test
    fun message_roundTrip_preservesDataWithoutAttachments() {
        val original = Message(
            id = "msg-rt2",
            conversationId = "conv-rt",
            role = Role.ASSISTANT,
            content = "Plain text",
            timestamp = 10000L
        )

        val result = original.toEntity().toDomain()

        assertEquals(original, result)
    }
}
