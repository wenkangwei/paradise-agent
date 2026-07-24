package com.example.aichat.data.remote

import app.cash.turbine.test
import com.example.aichat.data.provider.StreamEvent
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.test.runTest
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.atomic.AtomicBoolean

class AiStreamClientTest {

    private val client = AiStreamClient()

    private fun sseBody(text: String): ResponseBody =
        text.toResponseBody("text/event-stream".toMediaType())

    private fun List<StreamEvent>.contentDeltas(): List<String> =
        filterIsInstance<StreamEvent.ContentDelta>().map { it.text }

    private fun List<StreamEvent>.reasoningDeltas(): List<String> =
        filterIsInstance<StreamEvent.ReasoningDelta>().map { it.text }

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

        val events = client.toEventFlow(body).toList()

        assertEquals(listOf("Hello", " world", "!"), events.contentDeltas())
    }

    @Test
    fun `emits reasoning deltas alongside content`() = runTest {
        val body = sseBody(buildString {
            append("data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"thinking...\"}}]}\n")
            append("data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n")
            append("data: [DONE]\n")
        })

        val events = client.toEventFlow(body).toList()

        assertEquals(listOf("thinking..."), events.reasoningDeltas())
        assertEquals(listOf("answer"), events.contentDeltas())
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

        val events = client.toEventFlow(body).toList()

        assertEquals(listOf("Hi"), events.contentDeltas())
    }

    @Test
    fun `stops at DONE marker`() = runTest {
        val body = sseBody(buildString {
            append("data: {\"choices\":[{\"delta\":{\"content\":\"A\"}}]}\n")
            append("data: {\"choices\":[{\"delta\":{\"content\":\"B\"}}]}\n")
            append("data: [DONE]\n")
            append("data: {\"choices\":[{\"delta\":{\"content\":\"C\"}}]}\n")
        })

        val events = client.toEventFlow(body).toList()

        assertEquals(listOf("A", "B"), events.contentDeltas())
    }

    @Test
    fun `emits Finish event with reason when present`() = runTest {
        val body = sseBody(buildString {
            append("data: {\"choices\":[{\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}]}\n")
        })

        val events = client.toEventFlow(body).toList()

        assertTrue("Expected Finish event", events.any { it is StreamEvent.Finish })
        val finish = events.filterIsInstance<StreamEvent.Finish>().single()
        assertEquals("stop", finish.reason)
    }

    @Test
    fun `emits Finish with null reason at DONE when no prior finish_reason`() = runTest {
        val body = sseBody(buildString {
            append("data: {\"choices\":[{\"delta\":{\"content\":\"ok\"}}]}\n")
            append("data: [DONE]\n")
        })

        val events = client.toEventFlow(body).toList()

        val finish = events.filterIsInstance<StreamEvent.Finish>().single()
        assertEquals(null, finish.reason)
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

        val events = client.toEventFlow(body).toList()

        assertEquals(listOf("only this"), events.contentDeltas())
    }

    @Test
    fun `handles data prefix with or without space`() = runTest {
        val body = sseBody(buildString {
            append("data:{\"choices\":[{\"delta\":{\"content\":\"nospace\"}}]}\n")
            append("data: {\"choices\":[{\"delta\":{\"content\":\"space\"}}]}\n")
            append("data: [DONE]\n")
        })

        val events = client.toEventFlow(body).toList()

        assertEquals(listOf("nospace", "space"), events.contentDeltas())
    }

    @Test
    fun `completes on empty stream`() = runTest {
        val body = sseBody("")

        client.toEventFlow(body).test {
            awaitComplete()
        }
    }

    @Test
    fun `closes response body on completion`() = runTest {
        val closed = AtomicBoolean(false)
        val body = sseBody("data: {\"choices\":[{\"delta\":{\"content\":\"X\"}}]}\ndata: [DONE]\n")

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

        client.toEventFlow(trackingBody).test {
            assertEquals("X", (awaitItem() as StreamEvent.ContentDelta).text)
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

        val events = client.toEventFlow(body).toList()

        assertEquals(listOf("valid"), events.contentDeltas())
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

        val events = client.toEventFlow(body).toList()

        assertEquals(listOf("A", "B", "C"), events.contentDeltas())
    }
}
