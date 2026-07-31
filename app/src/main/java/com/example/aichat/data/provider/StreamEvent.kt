package com.example.aichat.data.provider

import com.example.aichat.data.remote.dto.ToolCardDto

sealed interface StreamEvent {
    data class ContentDelta(val text: String) : StreamEvent
    data class ReasoningDelta(val text: String) : StreamEvent
    data class ToolCall(val name: String, val args: String) : StreamEvent
    data class ToolCards(val cards: List<ToolCardDto>) : StreamEvent
    data class Finish(val reason: String?) : StreamEvent
    data object Cancelled : StreamEvent
}
