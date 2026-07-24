package com.example.aichat.ui.chat

/**
 * One-time UI events that should not be part of the persistent state.
 * Delivered via SharedFlow and consumed by the screen.
 */
sealed class ChatEvent {
    /**
     * An error occurred. The UI should show a snackbar.
     * [retryAction] is non-null when the operation can be retried.
     */
    data class ShowError(
        val message: String,
        val retryAction: (() -> Unit)? = null
    ) : ChatEvent()

    /**
     * A message was sent successfully and the AI response completed.
     */
    object MessageSent : ChatEvent()

    /**
     * Navigate to a specific conversation.
     */
    data class NavigateToConversation(val conversationId: String) : ChatEvent()
}
