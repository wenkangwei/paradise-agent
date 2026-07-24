package com.example.aichat.data.remote

import app.cash.turbine.test
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.test.runTest
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Test
import java.util.concurrent.atomic.AtomicBoolean

class AiStreamClientTest {

    private val client = AiStreamClient()

    private fun sseBody(text: String): ResponseBody =
        text.toResponseBody("text/event-stream".toMediaType())

    @Test
    fun `emits content deltas from valid SSE data lines in order`() = runTest {
        val body = sseBody(buildString {
            append("data: {\"choices\":[{\"delta\":{\"content\":\"Hello\"}}]}\n")
            append("\n")
            append("data: {\"choices\":[{\"delta\":{\"content\":\" world\"}}]}\n")
            append("\n")
            append("data: {\"choices\":[{\"delta\":{\"content\":\"!\"}}]}\n")
            append("\n")
            append("data: [DONE]\n")
        })

        val tokens = client.toTokenFlow(body).toList()

        assertEquals(listOf("Hello", " world", "!"), tokens)
    }

    @Test
    fun `skips non-data lines and empty lines`() = runTest {
        val body = sseBody(buildString {
            append(": this is a comment\n")
            append("event: chat\n")
            append("id: 12345\n")
            append("\n")
            append("data: {\"choices\":[{\"delta\":{\"content\":\"Hi\"}}]}\n")
            append("\n")
            append("data: [DONE]\n")
        })

        val tokens = client.toTokenFlow(body).toList()

        assertEquals(listOf("Hi"), tokens)
    }

    @Test
    fun `stops at DONE marker`() = runTest {
        val body = sseBody(buildString {
            append("data: {\"choices\":[{\"delta\":{\"content\":\"A\"}}]}\n")
            append("data: {\"choices\":[{\"delta\":{\"content\":\"B\"}}]}\n")
            append("data: [DONE]\n")
            append("data: {\"choices\":[{\"delta\":{\"content\":\"C\"}}]}\n")
        })

        val tokens = client.toTokenFlow(body).toList()

        assertEquals(listOf("A", "B"), tokens)
    }

    @Test
    fun `skips chunks with null or empty content delta`() = runTest {
        val body = sseBody(buildString {
            append("data: {\"choices\":[{\"delta\":{\"role\":\"assistant\"}}]}\n")
            append("\n")
            append("data: {\"choices\":[{\"delta\":{\"content\":\"only this\"}}]}\n")
            append("\n")
            append("data: {\"choices\":[{\"delta\":{\"content\":\"\"}}]}\n")
            append("\n")
            append("data: [DONE]\n")
        })

        val tokens = client.toTokenFlow(body).toList()

        assertEquals(listOf("only this"), tokens)
    }

    @Test
    fun `handles data prefix with or without space`() = runTest {
        val body = sseBody(buildString {
            append("data:{\"choices\":[{\"delta\":{\"content\":\"nospace\"}}]}\n")
            append("data: {\"choices\":[{\"delta\":{\"content\":\"space\"}}]}\n")
            append("data: [DONE]\n")
        })

        val tokens = client.toTokenFlow(body).toList()

        assertEquals(listOf("nospace", "space"), tokens)
    }

    @Test
    fun `completes on empty stream`() = runTest {
        val body = sseBody("")

        client.toTokenFlow(body).test {
            awaitComplete()
        }
    }

    @Test
    fun `closes response body on completion`() = runTest {
        val closed = AtomicBoolean(false)
        val body = sseBody("data: {\"choices\":[{\"delta\":{\"content\":\"X\"}}]}\ndata: [DONE]\n")

        // Wrap the close call to track invocation
        val originalSource = body.source()
        val trackingBody = object : ResponseBody() {
            override fun contentType() = body.contentType()
            override fun contentLength() = body.contentLength()
            override fun source() = originalSource
            override fun close() {
                closed.set(true)
                body.close()
            }
        }

        client.toTokenFlow(trackingBody).test {
            assertEquals("X", awaitItem())
            awaitComplete()
        }

        assertEquals(true, closed.get())
    }

    @Test
    fun `emits nothing for malformed JSON in data lines`() = runTest {
        val body = sseBody(buildString {
            append("data: {broken json}\n")
            append("data: {\"choices\":[{\"delta\":{\"content\":\"valid\"}}]}\n")
            append("data: [DONE]\n")
        })

        val tokens = client.toTokenFlow(body).toList()

        assertEquals(listOf("valid"), tokens)
    }

    @Test
    fun `multiple SSE events with blank line delimiters emit in order`() = runTest {
        val body = sseBody(buildString {
            append("event: message\n")
            append("data: {\"choices\":[{\"delta\":{\"content\":\"A\"}}]}\n")
            append("\n")
            append("event: message\n")
            append("data: {\"choices\":[{\"delta\":{\"content\":\"B\"}}]}\n")
            append("\n")
            append("event: message\n")
            append("data: {\"choices\":[{\"delta\":{\"content\":\"C\"}}]}\n")
            append("\n")
            append("data: [DONE]\n")
        })

        val tokens = client.toTokenFlow(body).toList()

        assertEquals(listOf("A", "B", "C"), tokens)
    }
}
