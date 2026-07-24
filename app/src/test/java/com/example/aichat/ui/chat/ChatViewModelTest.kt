package com.example.aichat.ui.chat

import android.content.Context
import app.cash.turbine.test
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
 * Tests cover:
 * - sendMessage with text-only and multimodal attachments
 * - stopGenerating cancellation
 * - Error handling (NetworkException, ApiException, generic)
 * - newChat / selectConversation / deleteConversation
 * - Pending attachment management
 *
 * Framework: JUnit4 + MockK + kotlinx-coroutines-test + Turbine
 *
 * Note: streamChat/streamChatMultimodal return Flow<String> (not suspend),
 * so we use `every {}` (not coEvery) to mock them.
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

    // ---------------------------------------------------------------------------
    // Test 1: sendMessage(text) - first send creates conversation, streams response
    // ---------------------------------------------------------------------------
    @Test
    fun `sendMessage first send creates conversation and streams AI response`() = runTest(testDispatcher) {
        // Arrange
        val conversationId = "conv-1"
        coEvery { repository.createConversation() } returns conversationId
        coEvery { repository.getMessages(conversationId) } returns emptyList()
        coEvery { repository.observeMessages(conversationId) } returns flowOf(emptyList())
        every { repository.observeConversations() } returns flowOf(emptyList())
        // streamChat returns a Flow (not suspend), use every {}
        every { remoteDataSource.streamChat(any(), any()) } returns flowOf("Hello", " ", "World")

        // Act
        viewModel.sendMessage("Hi")
        advanceUntilIdle()

        // Assert
        // User message + AI message persisted
        coVerify(exactly = 2) { repository.appendMessage(any()) }

        val state = viewModel.uiState.value
        val aiMessage = state.messages.lastOrNull()
        assertNotNull(aiMessage)
        assertEquals("Hello World", aiMessage?.content)
        assertFalse(state.isStreaming)
        assertFalse(state.isLoading)
    }

    // ---------------------------------------------------------------------------
    // Test 2: sendMessage with attachments uses multimodal endpoint
    // ---------------------------------------------------------------------------
    @Test
    fun `sendMessage with attachments calls streamChatMultimodal not streamChat`() = runTest(testDispatcher) {
        // Arrange
        val conversationId = "conv-2"
        coEvery { repository.createConversation() } returns conversationId

        // After persisting the user message with attachments, getMessages returns history with attachments
        // The exact id doesn't matter - what matters is that attachments are present to trigger multimodal
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

        // Return messages with attachments when getMessages is called
        coEvery { repository.getMessages(conversationId) } returns listOf(userMessage)
        coEvery { repository.observeMessages(conversationId) } returns flowOf(listOf(userMessage))
        every { repository.observeConversations() } returns flowOf(emptyList())

        coEvery { AttachmentEncoder.toDataUrl(any(), any()) } returns "data:image/jpeg;base64,abc123"
        every { remoteDataSource.streamChatMultimodal(any(), any()) } returns flowOf("Nice", " ", "image")

        val attachment = com.example.aichat.ui.chat.model.Attachment(
            id = "att-1",
            uri = "content://media/pic.jpg",
            mimeType = "image/jpeg",
            displayName = "pic.jpg"
        )

        // Act
        viewModel.sendMessage("Check this image", listOf(attachment))
        advanceUntilIdle()

        // Assert
        verify { remoteDataSource.streamChatMultimodal(any(), any()) }
        verify(exactly = 0) { remoteDataSource.streamChat(any(), any()) }

        val state = viewModel.uiState.value
        val aiMessage = state.messages.lastOrNull()
        assertNotNull(aiMessage)
        assertEquals("Nice image", aiMessage?.content)
    }

    // ---------------------------------------------------------------------------
    // Test 3: stopGenerating cancels streaming and persists partial content
    // ---------------------------------------------------------------------------
    @Test
    fun `stopGenerating cancels stream and persists partial content`() = runTest(testDispatcher) {
        // Arrange
        val conversationId = "conv-3"
        coEvery { repository.createConversation() } returns conversationId
        coEvery { repository.getMessages(conversationId) } returns emptyList()
        coEvery { repository.observeMessages(conversationId) } returns flowOf(emptyList())
        every { repository.observeConversations() } returns flowOf(emptyList())

        // Simulate a slow stream: emits "Part" then "ial", then hangs on delay
        every { remoteDataSource.streamChat(any(), any()) } returns flow {
            emit("Part")
            emit("ial")
            delay(60000) // Long delay - will be cancelled by stopGenerating
        }

        // Act - start sending, let the stream emit tokens but not complete the delay
        viewModel.sendMessage("Tell me a story")
        // advanceTimeBy runs scheduled coroutines up to 5s virtual time.
        // The stream emits "Part" and "ial" immediately, then suspends on delay(60000).
        // At 5s virtual time, the stream is still suspended in delay.
        advanceTimeBy(5000)

        // Verify streaming content has been received
        val midStreamMessages = viewModel.uiState.value.messages
        assertTrue("Stream should have started", midStreamMessages.isNotEmpty())

        viewModel.stopGenerating()
        advanceUntilIdle()

        // Assert
        assertFalse(viewModel.uiState.value.isLoading)

        // Should have persisted: user message + partial AI message (with "Partial" content)
        // The "Partial" content (from "Part" + "ial") should have been saved via NonCancellable
        coVerify(atLeast = 2) { repository.appendMessage(any()) }
    }

    // ---------------------------------------------------------------------------
    // Test 4: NetworkException produces ShowError event with retryAction
    // ---------------------------------------------------------------------------
    @Test
    fun `sendMessage NetworkException emits ShowError with retry`() = runTest(testDispatcher) {
        // Arrange
        val conversationId = "conv-4"
        coEvery { repository.createConversation() } returns conversationId
        coEvery { repository.getMessages(conversationId) } returns emptyList()
        coEvery { repository.observeMessages(conversationId) } returns flowOf(emptyList())
        every { repository.observeConversations() } returns flowOf(emptyList())
        // streamChat returns a Flow that throws when collected
        every { remoteDataSource.streamChat(any(), any()) } returns flow {
            throw NetworkException("Connection failed")
        }

        // Act & Assert via Turbine
        viewModel.events.test {
            viewModel.sendMessage("Hello")
            advanceUntilIdle()

            val event = awaitItem()
            assertTrue(event is ChatEvent.ShowError)
            val showError = event as ChatEvent.ShowError
            assertEquals("Connection failed", showError.message)
            assertNotNull(showError.retryAction)

            // State should have error set and not loading
            assertFalse(viewModel.uiState.value.isLoading)
            assertEquals("Connection failed", viewModel.uiState.value.error)

            cancelAndConsumeRemainingEvents()
        }
    }

    // ---------------------------------------------------------------------------
    // Test 4b: ApiException produces ShowError event with retryAction
    // ---------------------------------------------------------------------------
    @Test
    fun `sendMessage ApiException emits ShowError with retry`() = runTest(testDispatcher) {
        // Arrange
        val conversationId = "conv-5"
        coEvery { repository.createConversation() } returns conversationId
        coEvery { repository.getMessages(conversationId) } returns emptyList()
        coEvery { repository.observeMessages(conversationId) } returns flowOf(emptyList())
        every { repository.observeConversations() } returns flowOf(emptyList())
        every { remoteDataSource.streamChat(any(), any()) } returns flow {
            throw ApiException("HTTP 500")
        }

        // Act & Assert
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

    // ---------------------------------------------------------------------------
    // Test 4c: Generic exception produces ShowError with null retryAction
    // ---------------------------------------------------------------------------
    @Test
    fun `sendMessage generic exception emits ShowError without retry`() = runTest(testDispatcher) {
        // Arrange
        val conversationId = "conv-6"
        coEvery { repository.createConversation() } returns conversationId
        coEvery { repository.getMessages(conversationId) } returns emptyList()
        coEvery { repository.observeMessages(conversationId) } returns flowOf(emptyList())
        every { repository.observeConversations() } returns flowOf(emptyList())
        every { remoteDataSource.streamChat(any(), any()) } returns flow {
            throw RuntimeException("Unexpected")
        }

        // Act & Assert
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

    // ---------------------------------------------------------------------------
    // Test 5: newChat() resets UI state
    // ---------------------------------------------------------------------------
    @Test
    fun `newChat clears messages and conversationId`() = runTest(testDispatcher) {
        // Arrange - set up some state first
        val conversationId = "conv-7"
        coEvery { repository.createConversation() } returns conversationId
        coEvery { repository.getMessages(conversationId) } returns emptyList()
        coEvery { repository.observeMessages(conversationId) } returns flowOf(emptyList())
        every { repository.observeConversations() } returns flowOf(emptyList())
        every { remoteDataSource.streamChat(any(), any()) } returns flowOf("Response")

        viewModel.sendMessage("Hello")
        advanceUntilIdle()

        // Sanity: messages should be non-empty
        assertTrue(viewModel.uiState.value.messages.isNotEmpty())

        // Act
        viewModel.newChat()
        advanceUntilIdle()

        // Assert
        val state = viewModel.uiState.value
        assertTrue(state.messages.isEmpty())
        assertNull(state.currentConversationId)
        assertFalse(state.isLoading)
        assertFalse(state.isStreaming)
        assertNull(state.error)
        assertTrue(state.pendingAttachments.isEmpty())
    }

    // ---------------------------------------------------------------------------
    // Test 6: selectConversation(id) loads messages from repository
    // ---------------------------------------------------------------------------
    @Test
    fun `selectConversation loads messages from repository`() = runTest(testDispatcher) {
        // Arrange
        val conversationId = "conv-8"
        val messages = listOf(
            Message("m1", conversationId, Role.USER, "Hello", 1000L),
            Message("m2", conversationId, Role.ASSISTANT, "Hi there", 2000L)
        )
        coEvery { repository.observeMessages(conversationId) } returns flowOf(messages)
        every { repository.observeConversations() } returns flowOf(emptyList())

        // Act
        viewModel.selectConversation(conversationId)
        advanceUntilIdle()

        // Assert
        assertEquals(conversationId, viewModel.uiState.value.currentConversationId)
        assertEquals(2, viewModel.uiState.value.messages.size)
        assertEquals("Hello", viewModel.uiState.value.messages[0].content)
        assertEquals("Hi there", viewModel.uiState.value.messages[1].content)
        assertFalse(viewModel.uiState.value.isLoading)
    }

    // ---------------------------------------------------------------------------
    // Test 7: addPendingAttachment / removePendingAttachment / clearPendingAttachments
    // ---------------------------------------------------------------------------
    @Test
    fun `addPendingAttachment adds to pendingAttachments`() {
        // Act
        viewModel.addPendingAttachment("content://media/1", "image/png")

        // Assert
        assertEquals(1, viewModel.uiState.value.pendingAttachments.size)
        assertEquals("content://media/1", viewModel.uiState.value.pendingAttachments[0].uri)
        assertEquals("image/png", viewModel.uiState.value.pendingAttachments[0].mimeType)
    }

    @Test
    fun `removePendingAttachment removes from pendingAttachments`() {
        // Arrange
        viewModel.addPendingAttachment("content://media/1", "image/png")
        val id = viewModel.uiState.value.pendingAttachments[0].id

        // Act
        viewModel.removePendingAttachment(id)

        // Assert
        assertTrue(viewModel.uiState.value.pendingAttachments.isEmpty())
    }

    @Test
    fun `clearPendingAttachments removes all pending`() {
        // Arrange
        viewModel.addPendingAttachment("content://media/1", "image/png")
        viewModel.addPendingAttachment("content://media/2", "image/jpeg")

        // Act
        viewModel.clearPendingAttachments()

        // Assert
        assertTrue(viewModel.uiState.value.pendingAttachments.isEmpty())
    }

    // ---------------------------------------------------------------------------
    // Test 8: clearError resets error state
    // ---------------------------------------------------------------------------
    @Test
    fun `clearError sets error to null`() = runTest(testDispatcher) {
        // Arrange - trigger an error
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

        // Act
        viewModel.clearError()

        // Assert
        assertNull(viewModel.uiState.value.error)
    }

    // ---------------------------------------------------------------------------
    // Test 9: sendMessage ignores blank text with no attachments
    // ---------------------------------------------------------------------------
    @Test
    fun `sendMessage with blank text and no attachments is ignored`() = runTest(testDispatcher) {
        // Act
        viewModel.sendMessage("   ")
        advanceUntilIdle()

        // Assert - no interactions with repository beyond the init observer
        coVerify(exactly = 0) { repository.appendMessage(any()) }
        coVerify(exactly = 0) { repository.createConversation() }
    }

    // ---------------------------------------------------------------------------
    // Test 10: deleteConversation delegates to repository
    // ---------------------------------------------------------------------------
    @Test
    fun `deleteConversation calls repository deleteConversation`() = runTest(testDispatcher) {
        // Arrange
        val conversationId = "conv-10"
        coEvery { repository.deleteConversation(any()) } just Runs
        every { repository.observeConversations() } returns flowOf(emptyList())

        // Act
        viewModel.deleteConversation(conversationId)
        advanceUntilIdle()

        // Assert
        coVerify { repository.deleteConversation(conversationId) }
    }

    @Test
    fun `deleteConversation current conversation resets to new chat`() = runTest(testDispatcher) {
        // Arrange
        val conversationId = "conv-11"
        val messages = listOf(Message("m1", conversationId, Role.USER, "Hi", 1000L))
        coEvery { repository.observeMessages(conversationId) } returns flowOf(messages)
        coEvery { repository.deleteConversation(any()) } just Runs
        every { repository.observeConversations() } returns flowOf(emptyList())

        viewModel.selectConversation(conversationId)
        advanceUntilIdle()
        assertEquals(conversationId, viewModel.uiState.value.currentConversationId)

        // Act
        viewModel.deleteConversation(conversationId)
        advanceUntilIdle()

        // Assert
        assertNull(viewModel.uiState.value.currentConversationId)
        assertTrue(viewModel.uiState.value.messages.isEmpty())
    }

    // ---------------------------------------------------------------------------
    // Test 11: MessageSent event emitted on successful stream completion
    // ---------------------------------------------------------------------------
    @Test
    fun `successful stream emits MessageSent event`() = runTest(testDispatcher) {
        // Arrange
        val conversationId = "conv-12"
        coEvery { repository.createConversation() } returns conversationId
        coEvery { repository.getMessages(conversationId) } returns emptyList()
        coEvery { repository.observeMessages(conversationId) } returns flowOf(emptyList())
        every { repository.observeConversations() } returns flowOf(emptyList())
        every { remoteDataSource.streamChat(any(), any()) } returns flowOf("OK")

        // Act & Assert
        viewModel.events.test {
            viewModel.sendMessage("Hello")
            advanceUntilIdle()

            val event = awaitItem()
            assertTrue(event is ChatEvent.MessageSent)

            cancelAndConsumeRemainingEvents()
        }
    }
}
