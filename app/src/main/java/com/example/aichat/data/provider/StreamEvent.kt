package com.example.aichat.data.provider

/**
 * Discriminated stream events emitted by [LlmProvider.stream].
 *
 * The Flow<StreamEvent> replaces the old Flow<String> — callers can now react
 * to reasoning deltas, tool calls and finish signals separately from content.
 */
sealed interface StreamEvent {
    /** A chunk of visible assistant content (token delta). */
    data class ContentDelta(val text: String) : StreamEvent

    /** A chunk of reasoning text (e.g. DeepSeek-R1 `reasoning_content`). */
    data class ReasoningDelta(val text: String) : StreamEvent

    /**
     * The model is invoking a tool. (Reserved for future tool-calling support;
     * current UI ignores this event type.)
     */
    data class ToolCall(val name: String, val args: String) : StreamEvent

    /** Stream ended normally. Carries the OpenAI finish_reason if present. */
    data class Finish(val reason: String?) : StreamEvent

    /**
     * Stream was cancelled by the user. Emitted just before the Flow completes
     * when cancellation is detected inside the parser, so callers can persist
     * partial content under the right [com.example.aichat.domain.model.MessageStatus].
     */
    data object Cancelled : StreamEvent
}
