package com.example.aichat.data.remote

import app.cash.turbine.test
import com.example.aichat.data.local.entity.ApiProfileEntity
import com.example.aichat.data.provider.LlmProvider
import com.example.aichat.data.provider.LlmProviderFactory
import com.example.aichat.data.provider.StreamEvent
import com.example.aichat.data.repository.ApiProfile
import com.example.aichat.data.repository.ApiProfileRepository
import com.example.aichat.data.security.ApiKeyEncryptor
import com.example.aichat.domain.model.Message
import com.example.aichat.domain.model.Role
import io.mockk.coEvery
import io.mockk.mockk
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class ChatRemoteDataSourceTest {

    private val providerFactory: LlmProviderFactory = mockk()
    private val apiProfileRepo: ApiProfileRepository = mockk()
    private val apiKeyEncryptor: ApiKeyEncryptor = mockk()

    private lateinit var dataSource: ChatRemoteDataSource

    private val activeProfile = ApiProfile(
        id = "p1",
        title = "test",
        supplierId = "openai",
        baseUrl = "https://api.openai.com/v1/",
        modelName = "gpt-4o-mini",
        isDefault = true,
        hasApiKey = true,
        createdAt = 0,
        updatedAt = 0
    )

    private val testMessages = listOf(
        Message(
            id = "1",
            conversationId = "conv1",
            role = Role.USER,
            content = "Hello",
            timestamp = 0
        )
    )

    @Before
    fun setUp() = runTest {
        coEvery { apiProfileRepo.getActive() } returns activeProfile
        coEvery { apiKeyEncryptor.decrypt("p1") } returns "plain-key"
        dataSource = ChatRemoteDataSource(providerFactory, apiProfileRepo, apiKeyEncryptor)
    }

    private fun stubProvider(vararg events: StreamEvent): LlmProvider {
        val provider = mockk<LlmProvider>()
        val flow = if (events.size == 1) flowOf(events.first()) else flow { events.forEach { emit(it) } }
        // Match any ApiProfileEntity since the data source reconstructs one internally
        coEvery { provider.stream(any(), any(), any()) } returns flow
        coEvery { providerFactory.get(any<ApiProfileEntity>()) } returns provider
        return provider
    }

    @Test
    fun `streamChat emits content deltas in order`() = runTest {
        stubProvider(
            StreamEvent.ContentDelta("Hello"),
            StreamEvent.ContentDelta(" world"),
            StreamEvent.ContentDelta("!"),
            StreamEvent.Finish(reason = "stop")
        )

        dataSource.streamChat(testMessages).test {
            assertEquals("Hello", (awaitItem() as StreamEvent.ContentDelta).text)
            assertEquals(" world", (awaitItem() as StreamEvent.ContentDelta).text)
            assertEquals("!", (awaitItem() as StreamEvent.ContentDelta).text)
            assertTrue(awaitItem() is StreamEvent.Finish)
            awaitComplete()
        }
    }

    @Test
    fun `streamChat propagates reasoning deltas`() = runTest {
        stubProvider(
            StreamEvent.ReasoningDelta("thinking..."),
            StreamEvent.ContentDelta("answer"),
            StreamEvent.Finish(reason = "stop")
        )

        dataSource.streamChat(testMessages).test {
            assertEquals("thinking...", (awaitItem() as StreamEvent.ReasoningDelta).text)
            assertEquals("answer", (awaitItem() as StreamEvent.ContentDelta).text)
            assertTrue(awaitItem() is StreamEvent.Finish)
            awaitComplete()
        }
    }

    @Test
    fun `streamChatMultimodal passes attachments through`() = runTest {
        stubProvider(
            StreamEvent.ContentDelta("It's a cat"),
            StreamEvent.Finish(reason = null)
        )

        val multimodalMessages = listOf(
            Message(
                id = "1",
                conversationId = "conv1",
                role = Role.USER,
                content = "What is in this image?",
                timestamp = 0
            ) to listOf(
                Attachment(url = "data:image/jpeg;base64,/9j/4AAQ=", mimeType = "image/jpeg")
            )
        )

        dataSource.streamChatMultimodal(multimodalMessages).test {
            assertEquals("It's a cat", (awaitItem() as StreamEvent.ContentDelta).text)
            assertTrue(awaitItem() is StreamEvent.Finish)
            awaitComplete()
        }
    }

    @Test
    fun `streamChat handles empty attachments list`() = runTest {
        stubProvider(
            StreamEvent.ContentDelta("response"),
            StreamEvent.Finish(reason = null)
        )

        val messages = listOf(
            Message("1", "c", Role.USER, "Plain text", 0) to emptyList<Attachment>()
        )

        dataSource.streamChatMultimodal(messages).test {
            assertEquals("response", (awaitItem() as StreamEvent.ContentDelta).text)
            assertTrue(awaitItem() is StreamEvent.Finish)
            awaitComplete()
        }
    }
}
