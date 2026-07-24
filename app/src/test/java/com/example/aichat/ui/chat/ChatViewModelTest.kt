package com.example.aichat.ui.chat

import android.content.Context
import app.cash.turbine.test
import com.example.aichat.data.provider.StreamEvent
import com.example.aichat.data.remote.ApiException
import com.example.aichat.data.remote.AttachmentEncoder
import com.example.aichat.data.remote.ChatRemoteDataSource
import com.example.aichat.data.remote.NetworkException
import com.example.aichat.domain.model.Message
import com.example.aichat.domain.model.Role
import com.example.aichat.domain.repository.ChatRepository
import io.mockk.Runs
import io.mockk.coEvery
import io.mockk.coVerify
import io.mockk.every
import io.mockk.just
import io.mockk.mockk
import io.mockk.mockkObject
import io.mockk.unmockkObject
import io.mockk.verify
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * JVM unit tests for [ChatViewModel].
 *
 * The stream contract is now `Flow<StreamEvent>` (was `Flow<String>`). Tests
 * emit [StreamEvent.ContentDelta] / [StreamEvent.ReasoningDelta] / [StreamEvent.Finish]
 * to exercise the new dispatch logic in sendMessage.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class ChatViewModelTest {

    private val repository: ChatRepository = mockk(relaxed = true)
    private val remoteDataSource: ChatRemoteDataSource = mockk(relaxed = true)
    private val context: Context = mockk(relaxed = true)

    private val testDispatcher = StandardTestDispatcher()
    private lateinit var viewModel: ChatViewModel

    @Before
    fun setUp() {
        Dispatchers.setMain(testDispatcher)
        mockkObject(AttachmentEncoder)
        viewModel = ChatViewModel(repository, remoteDataSource, context)
    }

    @After
    fun tearDown() {
        unmockkObject(AttachmentEncoder)
        Dispatchers.resetMain()
    }

    private fun contentFlow(vararg tokens: String) =
        flowOf(*tokens.map { StreamEvent.ContentDelta(it) }.toTypedArray(), StreamEvent.Finish("stop"))

    @Test
    fun `sendMessage first send creates conversation and streams AI response`() = runTest(testDispatcher) {
        val conversationId = "conv-1"
        coEvery { repository.createConversation() } returns conversationId
        coEvery { repository.getMessages(conversationId) } returns emptyList()
        coEvery { repository.observeMessages(conversationId) } returns flowOf(emptyList())
        every { repository.observeConversations() } returns flowOf(emptyList())
        every { remoteDataSource.streamChat(any(), any()) } returns contentFlow("Hello", " ", "World")

        viewModel.sendMessage("Hi")
        advanceUntilIdle()

        coVerify(exactly = 2) { repository.appendMessage(any()) }
        val state = viewModel.uiState.value
        val aiMessage = state.messages.lastOrNull()
        assertNotNull(aiMessage)
        assertEquals("Hello World", aiMessage?.content)
        assertFalse(state.isStreaming)
        assertFalse(state.isLoading)
    }

    @Test
    fun `sendMessage with attachments calls streamChatMultimodal not streamChat`() = runTest(testDispatcher) {
        val conversationId = "conv-2"
        coEvery { repository.createConversation() } returns conversationId

        val userMessage = Message(
            id = "test-user-msg",
            conversationId = conversationId,
            role = Role.USER,
            content = "Check this image",
            timestamp = System.currentTimeMillis(),
            attachments = listOf(
                com.example.aichat.domain.model.Attachment("att-1", "image/jpeg", "data:image/jpeg;base64,abc123")
            )
        )

        coEvery { repository.getMessages(conversationId) } returns listOf(userMessage)
        coEvery { repository.observeMessages(conversationId) } returns flowOf(listOf(userMessage))
        every { repository.observeConversations() } returns flowOf(emptyList())
        coEvery { AttachmentEncoder.toDataUrl(any(), any()) } returns "data:image/jpeg;base64,abc123"
        every { remoteDataSource.streamChatMultimodal(any(), any()) } returns
            flowOf(StreamEvent.ContentDelta("Nice"), StreamEvent.ContentDelta(" image"), StreamEvent.Finish(null))

        val attachment = com.example.aichat.ui.chat.model.Attachment(
            id = "att-1",
            uri = "content://media/pic.jpg",
            mimeType = "image/jpeg",
            displayName = "pic.jpg"
        )

        viewModel.sendMessage("Check this image", listOf(attachment))
        advanceUntilIdle()

        verify { remoteDataSource.streamChatMultimodal(any(), any()) }
        verify(exactly = 0) { remoteDataSource.streamChat(any(), any()) }

        val aiMessage = viewModel.uiState.value.messages.lastOrNull()
        assertNotNull(aiMessage)
        assertEquals("Nice image", aiMessage?.content)
    }

    @Test
    fun `stopGenerating cancels stream and persists partial content`() = runTest(testDispatcher) {
        val conversationId = "conv-3"
        coEvery { repository.createConversation() } returns conversationId
        coEvery { repository.getMessages(conversationId) } returns emptyList()
        coEvery { repository.observeMessages(conversationId) } returns flowOf(emptyList())
        every { repository.observeConversations() } returns flowOf(emptyList())

        every { remoteDataSource.streamChat(any(), any()) } returns flow {
            emit(StreamEvent.ContentDelta("Part"))
            emit(StreamEvent.ContentDelta("ial"))
            delay(60000)
        }

        viewModel.sendMessage("Tell me a story")
        advanceTimeBy(5000)

        val midStreamMessages = viewModel.uiState.value.messages
        assertTrue("Stream should have started", midStreamMessages.isNotEmpty())

        viewModel.stopGenerating()
        advanceUntilIdle()

        assertFalse(viewModel.uiState.value.isLoading)
        coVerify(atLeast = 2) { repository.appendMessage(any()) }
    }

    @Test
    fun `sendMessage NetworkException emits ShowError with retry`() = runTest(testDispatcher) {
        val conversationId = "conv-4"
        coEvery { repository.createConversation() } returns conversationId
        coEvery { repository.getMessages(conversationId) } returns emptyList()
        coEvery { repository.observeMessages(conversationId) } returns flowOf(emptyList())
        every { repository.observeConversations() } returns flowOf(emptyList())
        every { remoteDataSource.streamChat(any(), any()) } returns flow {
            throw NetworkException("Connection failed")
        }

        viewModel.events.test {
            viewModel.sendMessage("Hello")
            advanceUntilIdle()

            val event = awaitItem()
            assertTrue(event is ChatEvent.ShowError)
            val showError = event as ChatEvent.ShowError
            assertEquals("Connection failed", showError.message)
            assertNotNull(showError.retryAction)

            assertFalse(viewModel.uiState.value.isLoading)
            assertEquals("Connection failed", viewModel.uiState.value.error)
            cancelAndConsumeRemainingEvents()
        }
    }

    @Test
    fun `sendMessage ApiException emits ShowError with retry`() = runTest(testDispatcher) {
        val conversationId = "conv-5"
        coEvery { repository.createConversation() } returns conversationId
        coEvery { repository.getMessages(conversationId) } returns emptyList()
        coEvery { repository.observeMessages(conversationId) } returns flowOf(emptyList())
        every { repository.observeConversations() } returns flowOf(emptyList())
        every { remoteDataSource.streamChat(any(), any()) } returns flow {
            throw ApiException("HTTP 500")
        }

        viewModel.events.test {
            viewModel.sendMessage("Hello")
            advanceUntilIdle()

            val event = awaitItem()
            assertTrue(event is ChatEvent.ShowError)
            val showError = event as ChatEvent.ShowError
            assertEquals("HTTP 500", showError.message)
            assertNotNull(showError.retryAction)
            cancelAndConsumeRemainingEvents()
        }
    }

    @Test
    fun `sendMessage generic exception emits ShowError without retry`() = runTest(testDispatcher) {
        val conversationId = "conv-6"
        coEvery { repository.createConversation() } returns conversationId
        coEvery { repository.getMessages(conversationId) } returns emptyList()
        coEvery { repository.observeMessages(conversationId) } returns flowOf(emptyList())
        every { repository.observeConversations() } returns flowOf(emptyList())
        every { remoteDataSource.streamChat(any(), any()) } returns flow {
            throw RuntimeException("Unexpected")
        }

        viewModel.events.test {
            viewModel.sendMessage("Hello")
            advanceUntilIdle()

            val event = awaitItem()
            assertTrue(event is ChatEvent.ShowError)
            val showError = event as ChatEvent.ShowError
            assertEquals("Unexpected", showError.message)
            assertNull(showError.retryAction)
            cancelAndConsumeRemainingEvents()
        }
    }

    @Test
    fun `newChat clears messages and conversationId`() = runTest(testDispatcher) {
        val conversationId = "conv-7"
        coEvery { repository.createConversation() } returns conversationId
        coEvery { repository.getMessages(conversationId) } returns emptyList()
        coEvery { repository.observeMessages(conversationId) } returns flowOf(emptyList())
        every { repository.observeConversations() } returns flowOf(emptyList())
        every { remoteDataSource.streamChat(any(), any()) } returns contentFlow("Response")

        viewModel.sendMessage("Hello")
        advanceUntilIdle()
        assertTrue(viewModel.uiState.value.messages.isNotEmpty())

        viewModel.newChat()
        advanceUntilIdle()

        val state = viewModel.uiState.value
        assertTrue(state.messages.isEmpty())
        assertNull(state.currentConversationId)
        assertFalse(state.isLoading)
        assertFalse(state.isStreaming)
        assertNull(state.error)
        assertTrue(state.pendingAttachments.isEmpty())
    }

    @Test
    fun `selectConversation loads messages from repository`() = runTest(testDispatcher) {
        val conversationId = "conv-8"
        val messages = listOf(
            Message("m1", conversationId, Role.USER, "Hello", 1000L),
            Message("m2", conversationId, Role.ASSISTANT, "Hi there", 2000L)
        )
        coEvery { repository.observeMessages(conversationId) } returns flowOf(messages)
        every { repository.observeConversations() } returns flowOf(emptyList())

        viewModel.selectConversation(conversationId)
        advanceUntilIdle()

        assertEquals(conversationId, viewModel.uiState.value.currentConversationId)
        assertEquals(2, viewModel.uiState.value.messages.size)
        assertEquals("Hello", viewModel.uiState.value.messages[0].content)
        assertEquals("Hi there", viewModel.uiState.value.messages[1].content)
        assertFalse(viewModel.uiState.value.isLoading)
    }

    @Test
    fun `addPendingAttachment adds to pendingAttachments`() {
        viewModel.addPendingAttachment("content://media/1", "image/png")
        assertEquals(1, viewModel.uiState.value.pendingAttachments.size)
        assertEquals("content://media/1", viewModel.uiState.value.pendingAttachments[0].uri)
        assertEquals("image/png", viewModel.uiState.value.pendingAttachments[0].mimeType)
    }

    @Test
    fun `removePendingAttachment removes from pendingAttachments`() {
        viewModel.addPendingAttachment("content://media/1", "image/png")
        val id = viewModel.uiState.value.pendingAttachments[0].id
        viewModel.removePendingAttachment(id)
        assertTrue(viewModel.uiState.value.pendingAttachments.isEmpty())
    }

    @Test
    fun `clearPendingAttachments removes all pending`() {
        viewModel.addPendingAttachment("content://media/1", "image/png")
        viewModel.addPendingAttachment("content://media/2", "image/jpeg")
        viewModel.clearPendingAttachments()
        assertTrue(viewModel.uiState.value.pendingAttachments.isEmpty())
    }

    @Test
    fun `clearError sets error to null`() = runTest(testDispatcher) {
        val conversationId = "conv-9"
        coEvery { repository.createConversation() } returns conversationId
        coEvery { repository.getMessages(conversationId) } returns emptyList()
        coEvery { repository.observeMessages(conversationId) } returns flowOf(emptyList())
        every { repository.observeConversations() } returns flowOf(emptyList())
        every { remoteDataSource.streamChat(any(), any()) } returns flow {
            throw NetworkException("Fail")
        }

        viewModel.sendMessage("test")
        advanceUntilIdle()
        assertNotNull(viewModel.uiState.value.error)

        viewModel.clearError()
        assertNull(viewModel.uiState.value.error)
    }

    @Test
    fun `sendMessage with blank text and no attachments is ignored`() = runTest(testDispatcher) {
        viewModel.sendMessage("   ")
        advanceUntilIdle()
        coVerify(exactly = 0) { repository.appendMessage(any()) }
        coVerify(exactly = 0) { repository.createConversation() }
    }

    @Test
    fun `deleteConversation calls repository deleteConversation`() = runTest(testDispatcher) {
        val conversationId = "conv-10"
        coEvery { repository.deleteConversation(any()) } just Runs
        every { repository.observeConversations() } returns flowOf(emptyList())

        viewModel.deleteConversation(conversationId)
        advanceUntilIdle()
        coVerify { repository.deleteConversation(conversationId) }
    }

    @Test
    fun `deleteConversation current conversation resets to new chat`() = runTest(testDispatcher) {
        val conversationId = "conv-11"
        val messages = listOf(Message("m1", conversationId, Role.USER, "Hi", 1000L))
        coEvery { repository.observeMessages(conversationId) } returns flowOf(messages)
        coEvery { repository.deleteConversation(any()) } just Runs
        every { repository.observeConversations() } returns flowOf(emptyList())

        viewModel.selectConversation(conversationId)
        advanceUntilIdle()
        assertEquals(conversationId, viewModel.uiState.value.currentConversationId)

        viewModel.deleteConversation(conversationId)
        advanceUntilIdle()

        assertNull(viewModel.uiState.value.currentConversationId)
        assertTrue(viewModel.uiState.value.messages.isEmpty())
    }

    @Test
    fun `successful stream emits MessageSent event`() = runTest(testDispatcher) {
        val conversationId = "conv-12"
        coEvery { repository.createConversation() } returns conversationId
        coEvery { repository.getMessages(conversationId) } returns emptyList()
        coEvery { repository.observeMessages(conversationId) } returns flowOf(emptyList())
        every { repository.observeConversations() } returns flowOf(emptyList())
        every { remoteDataSource.streamChat(any(), any()) } returns contentFlow("OK")

        viewModel.events.test {
            viewModel.sendMessage("Hello")
            advanceUntilIdle()

            val event = awaitItem()
            assertTrue(event is ChatEvent.MessageSent)
            cancelAndConsumeRemainingEvents()
        }
    }
}
