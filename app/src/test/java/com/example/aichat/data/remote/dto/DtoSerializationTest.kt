package com.example.aichat.data.remote.dto

import com.google.gson.Gson
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class DtoSerializationTest {

    private val gson = Gson()

    // --- ContentPart serialization ---

    @Test
    fun `ContentPart Text serializes with type and text fields`() {
        val part = ContentPart.Text(text = "Hello world")

        val json = gson.toJsonTree(part).asJsonObject

        assertEquals("text", json.get("type").asString)
        assertEquals("Hello world", json.get("text").asString)
    }

    @Test
    fun `ContentPart ImageUrl serializes with type and image_url fields`() {
        val part = ContentPart.ImageUrl(
            imageUrl = ImageUrlData(url = "https://example.com/img.png")
        )

        val json = gson.toJsonTree(part).asJsonObject

        assertEquals("image_url", json.get("type").asString)
        val imageUrl = json.getAsJsonObject("image_url")
        assertEquals("https://example.com/img.png", imageUrl.get("url").asString)
    }

    // --- ContentPart deserialization ---

    @Test
    fun `ContentPart deserializes text type correctly`() {
        val json = """{"type":"text","text":"Hi there"}"""

        val part = gson.fromJson(json, ContentPart::class.java)

        assertTrue("Expected ContentPart.Text", part is ContentPart.Text)
        assertEquals("Hi there", (part as ContentPart.Text).text)
    }

    @Test
    fun `ContentPart deserializes image_url type correctly`() {
        val json = """{"type":"image_url","image_url":{"url":"https://example.com/cat.jpg","detail":"high"}}"""

        val part = gson.fromJson(json, ContentPart::class.java)

        assertTrue("Expected ContentPart.ImageUrl", part is ContentPart.ImageUrl)
        val imageUrlPart = part as ContentPart.ImageUrl
        assertEquals("https://example.com/cat.jpg", imageUrlPart.imageUrl.url)
        assertEquals("high", imageUrlPart.imageUrl.detail)
    }

    @Test
    fun `ContentPart round-trip preserves text data`() {
        val original = ContentPart.Text(text = "Round trip")

        val json = gson.toJson(original)
        val restored = gson.fromJson(json, ContentPart::class.java)

        assertEquals(original, restored)
    }

    @Test
    fun `ContentPart round-trip preserves image_url data`() {
        val original = ContentPart.ImageUrl(
            imageUrl = ImageUrlData(url = "data:image/png;base64,abc==", detail = "low")
        )

        val json = gson.toJson(original)
        val restored = gson.fromJson(json, ContentPart::class.java)

        assertEquals(original, restored)
    }

    // --- MessageDto serialization ---

    @Test
    fun `MessageDto serializes text-only content as array`() {
        val dto = MessageDto(role = "user", content = "Hello")

        val json = gson.toJsonTree(dto).asJsonObject

        assertEquals("user", json.get("role").asString)
        val contentArray = json.getAsJsonArray("content")
        assertEquals(1, contentArray.size())
        val firstPart = contentArray[0].asJsonObject
        assertEquals("text", firstPart.get("type").asString)
        assertEquals("Hello", firstPart.get("text").asString)
    }

    @Test
    fun `MessageDto serializes multimodal content with text and image parts`() {
        val dto = MessageDto(
            role = "user",
            content = listOf(
                ContentPart.Text(text = "What is this?"),
                ContentPart.ImageUrl(imageUrl = ImageUrlData(url = "https://example.com/img.png"))
            )
        )

        val json = gson.toJsonTree(dto).asJsonObject
        val contentArray = json.getAsJsonArray("content")

        assertEquals(2, contentArray.size())

        val textPart = contentArray[0].asJsonObject
        assertEquals("text", textPart.get("type").asString)
        assertEquals("What is this?", textPart.get("text").asString)

        val imagePart = contentArray[1].asJsonObject
        assertEquals("image_url", imagePart.get("type").asString)
        assertEquals("https://example.com/img.png",
            imagePart.getAsJsonObject("image_url").get("url").asString)
    }

    // --- ChatRequestDto serialization ---

    @Test
    fun `ChatRequestDto serializes with model, messages, stream fields`() {
        val dto = ChatRequestDto(
            model = "gpt-4o-mini",
            messages = listOf(
                MessageDto(role = "system", content = "You are helpful"),
                MessageDto(role = "user", content = "Hi")
            ),
            stream = true
        )

        val json = gson.toJsonTree(dto).asJsonObject

        assertEquals("gpt-4o-mini", json.get("model").asString)
        assertEquals(true, json.get("stream").asBoolean)
        val messages = json.getAsJsonArray("messages")
        assertEquals(2, messages.size())
    }

    @Test
    fun `ChatRequestDto serializes multimodal request correctly`() {
        val dto = ChatRequestDto(
            model = "gpt-4o",
            messages = listOf(
                MessageDto(
                    role = "user",
                    content = listOf(
                        ContentPart.Text(text = "Describe this"),
                        ContentPart.ImageUrl(imageUrl = ImageUrlData(url = "data:image/jpeg;base64,xyz=="))
                    )
                )
            )
        )

        val json = gson.toJsonTree(dto).asJsonObject
        val firstMessage = json.getAsJsonArray("messages")[0].asJsonObject
        val content = firstMessage.getAsJsonArray("content")

        assertEquals(2, content.size())
        assertEquals("text", content[0].asJsonObject.get("type").asString)
        assertEquals("image_url", content[1].asJsonObject.get("type").asString)
    }

    // --- ChatStreamChunkDto deserialization ---

    @Test
    fun `ChatStreamChunkDto deserializes content delta from OpenAI SSE chunk`() {
        val json = """{
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "Hello"},
                    "finish_reason": null
                }
            ]
        }"""

        val chunk = gson.fromJson(json, ChatStreamChunkDto::class.java)

        assertEquals(1, chunk.choices.size)
        assertEquals("Hello", chunk.contentDelta)
    }

    @Test
    fun `ChatStreamChunkDto handles role-only delta with null content`() {
        val json = """{
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant"},
                    "finish_reason": null
                }
            ]
        }"""

        val chunk = gson.fromJson(json, ChatStreamChunkDto::class.java)

        assertEquals(null, chunk.contentDelta)
    }

    @Test
    fun `ChatStreamChunkDto handles finish_reason in delta`() {
        val json = """{
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop"
                }
            ]
        }"""

        val chunk = gson.fromJson(json, ChatStreamChunkDto::class.java)

        assertEquals("stop", chunk.choices[0].finishReason)
        assertEquals(null, chunk.contentDelta)
    }

    @Test
    fun `ChatStreamChunkDto handles empty choices array`() {
        val json = """{"choices": []}"""

        val chunk = gson.fromJson(json, ChatStreamChunkDto::class.java)

        assertEquals(0, chunk.choices.size)
        assertEquals(null, chunk.contentDelta)
    }

    @Test
    fun `ChatStreamChunkDto multiple chunks produce ordered deltas`() {
        val lines = listOf(
            """{"choices":[{"index":0,"delta":{"content":"A"},"finish_reason":null}]}""",
            """{"choices":[{"index":0,"delta":{"content":"B"},"finish_reason":null}]}""",
            """{"choices":[{"index":0,"delta":{"content":"C"},"finish_reason":null}]}"""
        )

        val deltas = lines.map { gson.fromJson(it, ChatStreamChunkDto::class.java).contentDelta }

        assertEquals(listOf("A", "B", "C"), deltas)
    }
}
