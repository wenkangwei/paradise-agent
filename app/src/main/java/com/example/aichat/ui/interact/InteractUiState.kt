package com.example.aichat.ui.interact

/**
 * UI state for the Live2D Interact tab.
 *
 * `phase` drives the bottom control bar's button states (e.g. 中断 button only
 * enabled when phase != Idle; record button shows different tint while recording).
 *
 * `history` accumulates all turns in-memory for the lifetime of the process —
 * MVP does not persist to Room. The floating bubble preview reads the last
 * entry; the expandable card shows the full list.
 */
data class InteractUiState(
    val phase: Phase = Phase.Idle,
    val history: List<ChatTurn> = emptyList(),
    /** Latest AI streamed text — convenience for the bubble preview while streaming. */
    val streamingAiText: String = "",
    /** Last error message surfaced via toast/snackbar; null = none. */
    val errorMessage: String? = null,
)

enum class Phase {
    Idle,
    Recording,
    Transcribing,
    Thinking,
    Speaking,
    ;

    val isBusy: Boolean get() = this != Idle
}

data class ChatTurn(
    val role: ChatRole,
    val text: String,
    val timestamp: Long,
)

enum class ChatRole(val label: String) {
    USER("我"),
    AI("角色"),
}
