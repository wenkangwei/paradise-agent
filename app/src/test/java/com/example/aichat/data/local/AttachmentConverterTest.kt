package com.example.aichat.data.local

import com.example.aichat.domain.model.Attachment
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AttachmentConverterTest {

    private val converter = AttachmentConverter()

    @Test
    fun fromAttachmentList_emptyList_producesEmptyJsonArray() {
        val result = converter.fromAttachmentList(emptyList())

        assertEquals("[]", result)
    }

    @Test
    fun toAttachmentList_emptyJsonArray_returnsEmptyList() {
        val result = converter.toAttachmentList("[]")

        assertTrue(result.isEmpty())
    }

    @Test
    fun toAttachmentList_blankString_returnsEmptyList() {
        val result = converter.toAttachmentList("")

        assertTrue(result.isEmpty())
    }

    @Test
    fun roundTrip_singleAttachment_preservesData() {
        val original = listOf(
            Attachment("att-1", "image/png", "content://photos/1")
        )

        val json = converter.fromAttachmentList(original)
        val result = converter.toAttachmentList(json)

        assertEquals(1, result.size)
        assertEquals(original[0], result[0])
    }

    @Test
    fun roundTrip_multipleAttachments_preservesData() {
        val original = listOf(
            Attachment("att-1", "image/png", "content://photos/1"),
            Attachment("att-2", "image/jpeg", "content://photos/2"),
            Attachment("att-3", "application/pdf", "content://docs/3")
        )

        val json = converter.fromAttachmentList(original)
        val result = converter.toAttachmentList(json)

        assertEquals(3, result.size)
        assertEquals(original[0], result[0])
        assertEquals(original[1], result[1])
        assertEquals(original[2], result[2])
    }

    @Test
    fun toAttachmentList_invalidJson_returnsEmptyList() {
        val result = converter.toAttachmentList("not valid json")

        assertTrue(result.isEmpty())
    }

    @Test
    fun fromAttachmentList_serializesAllFields() {
        val attachments = listOf(
            Attachment("id-x", "image/webp", "file:///path/to/img.webp")
        )

        val json = converter.fromAttachmentList(attachments)

        assertTrue(json.contains("\"id\":\"id-x\""))
        assertTrue(json.contains("\"mimeType\":\"image/webp\""))
        assertTrue(json.contains("\"uri\":\"file:///path/to/img.webp\""))
    }
}
