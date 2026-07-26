package com.example.aichat.ui.chat

import com.example.aichat.data.repository.ApiProfile
import com.example.aichat.data.repository.FavoriteTool
import com.example.aichat.domain.model.Conversation
import com.example.aichat.ui.chat.model.Attachment
import com.example.aichat.ui.chat.model.ChatMessage

data class ChatUiState(
    val messages: List<ChatMessage> = emptyList(),
    val isLoading: Boolean = false,
    val isStreaming: Boolean = false,
    val error: String? = null,
    val currentConversationId: String? = null,
    /** Title of the active conversation — shown as TopAppBar primary line. */
    val currentConversationTitle: String? = null,
    val conversations: List<Conversation> = emptyList(),
    val pendingAttachments: List<Attachment> = emptyList(),
    val profiles: List<ApiProfile> = emptyList(),
    val activeProfile: ApiProfile? = null,
    /**
     * ConversationIds that currently have a STREAMING row in Room.
     *
     * - Drives the green-dot indicator on the drawer's conversation items.
     * - Drives the concurrent-stream cap in `ChatViewModel.sendMessage`
     *   (rejects new sends beyond `MAX_CONCURRENT_STREAMS`).
     *
     * Sourced from `MessageDao.observeStreamingConversationIds`, which is
     * kept fresh across processes by Room's multi-instance invalidation.
     */
    val streamingConversationIds: Set<String> = emptySet(),
    /**
     * Pinned tool cards shown in the drawer's "收藏工具" section.
     * Backed by [FavoriteToolRepository.observeAll].
     */
    val favoriteTools: List<FavoriteTool> = emptyList(),
    /**
     * One-shot prefill payload for the input bar.
     *
     * Set when the user taps a favorite tool in the drawer ("use this tool"):
     * the tool's content lands in the input box so the user can edit / send
     * it as their next prompt. The ChatScreen reads AND clears this via
     * [ChatViewModel.consumePendingInput] — once consumed, it returns to null
     * so a configuration change doesn't re-inject the same text.
     */
    val pendingInput: String? = null
)
