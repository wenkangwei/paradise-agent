package com.example.aichat.data.remote

import app.cash.turbine.test
import com.example.aichat.data.remote.dto.ChatRequestDto
import com.example.aichat.domain.model.AppConfig
import com.example.aichat.domain.model.Message
import com.example.aichat.domain.model.Role
import io.mockk.coEvery
import io.mockk.every
import io.mockk.mockk
import kotlinx.coroutines.test.runTest
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import retrofit2.HttpException
import java.io.IOException

class ChatRemoteDataSourceTest {

    private val apiService: AiApiService = mockk()
    private val streamClient: AiStreamClient = AiStreamClient()
    private val configManager: ConfigManager = mockk()

    private lateinit var dataSource: ChatRemoteDataSource

    private val testMessages = listOf(
        Message(
            id = "1",
            conversationId = "conv1",
            role = Role.USER,
            content = "Hello",
            timestamp = 0
        )
    )

    private fun sseResponseBody(vararg tokens: String) =
        buildString {
            for (t in tokens) {
                append("data: {\"choices\":[{\"delta\":{\"content\":")
                append("\"")
                append(t)
                append("\"")
                append("}}]}\n\n")
            }
            append("data: [DONE]\n")
        }.toResponseBody("text/event-stream".toMediaType())

    @Before
    fun setUp() {
        every { configManager.currentConfig() } returns AppConfig(
            baseUrl = "https://api.openai.com/",
            apiKey = "test-key",
            model = "gpt-4o-mini"
        )
        dataSource = ChatRemoteDataSource(apiService, streamClient, configManager)
    }

    @Test
    fun `streamChat emits tokens from SSE response`() = runTest {
        coEvery { apiService.streamChat(any()) } returns sseResponseBody("Hello", " world", "!")

        dataSource.streamChat(testMessages).test {
            assertEquals("Hello", awaitItem())
            assertEquals(" world", awaitItem())
            assertEquals("!", awaitItem())
            awaitComplete()
        }
    }

    @Test
    fun `streamChat uses model override when provided`() = runTest {
        coEvery { apiService.streamChat(any()) } returns sseResponseBody("ok")

        dataSource.streamChat(testMessages, model = "gpt-4o").test {
            assertEquals("ok", awaitItem())
            awaitComplete()
        }
    }

    @Test
    fun `streamChat throws ApiException on HTTP 500`() = runTest {
        val httpException = mockk<HttpException>()
        every { httpException.code() } returns 500
        every { httpException.message() } returns "Internal Server Error"
        coEvery { apiService.streamChat(any()) } throws httpException

        dataSource.streamChat(testMessages).test {
            val error = awaitError()
            assertTrue("Expected ApiException, got ${error::class.simpleName}", error is ApiException)
        }
    }

    @Test
    fun `streamChat throws NetworkException on IOException`() = runTest {
        coEvery { apiService.streamChat(any()) } throws IOException("Connection timed out")

        dataSource.streamChat(testMessages).test {
            val error = awaitError()
            assertTrue("Expected NetworkException, got ${error::class.simpleName}", error is NetworkException)
        }
    }

    @Test
    fun `streamChatMultimodal emits tokens for text plus image message`() = runTest {
        val multimodalMessages = listOf(
            Message(
                id = "1",
                conversationId = "conv1",
                role = Role.USER,
                content = "What is in this image?",
                timestamp = 0,
                attachments = listOf()
            ) to listOf(
                Attachment(url = "data:image/jpeg;base64,/9j/4AAQ=", mimeType = "image/jpeg")
            )
        )

        coEvery { apiService.streamChat(any()) } returns sseResponseBody("It's a cat")

        dataSource.streamChatMultimodal(multimodalMessages).test {
            assertEquals("It's a cat", awaitItem())
            awaitComplete()
        }
    }

    @Test
    fun `streamChatMultimodal handles empty attachments list`() = runTest {
        val messages = listOf(
            Message(
                id = "1",
                conversationId = "conv1",
                role = Role.USER,
                content = "Plain text",
                timestamp = 0
            ) to emptyList<Attachment>()
        )

        coEvery { apiService.streamChat(any()) } returns sseResponseBody("response")

        dataSource.streamChatMultimodal(messages).test {
            assertEquals("response", awaitItem())
            awaitComplete()
        }
    }

    @Test
    fun `streamChatMultimodal throws ApiException on HTTP error`() = runTest {
        val httpException = mockk<HttpException>()
        every { httpException.code() } returns 401
        every { httpException.message() } returns "Unauthorized"
        coEvery { apiService.streamChat(any()) } throws httpException

        val messages = listOf(
            Message("1", "c", Role.USER, "hi", 0) to listOf(
                Attachment(url = "https://example.com/img.png")
            )
        )

        dataSource.streamChatMultimodal(messages).test {
            val error = awaitError()
            assertTrue("Expected ApiException, got ${error::class.simpleName}", error is ApiException)
        }
    }
}
