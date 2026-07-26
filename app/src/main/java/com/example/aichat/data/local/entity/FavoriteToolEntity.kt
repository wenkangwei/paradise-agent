package com.example.aichat.data.local.entity

import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey

/**
 * A "tool card" the user pinned from an AI reply.
 *
 * Tool cards are surfaced when an AI message contains either a complete HTML
 * page (<!DOCTYPE…</html>) or a long Markdown document (fenced ```markdown
 * block > 20 lines). The user can pin such a card to the drawer's "收藏工具"
 * section via the ⭐ button on the card. Pinned cards are reusable — tapping
 * one in the drawer prefills its content into the chat input bar so the user
 * can edit and re-send it as a prompt.
 *
 * @param content raw source. For HTML cards this is the original HTML; for
 *   Markdown cards this is the original markdown text. Shared verbatim via
 *   Intent.ACTION_SEND (HTML → .html file, Markdown → .md file).
 * @param type one of [TYPE_HTML] / [TYPE_MARKDOWN]. Drives the file
 *   extension when sharing and the rendering mode in the full-screen view.
 * @param sourceMessageId the message row the card was pinned from, for
 *   traceability. Null when the card was created independently.
 */
@Entity(
    tableName = "favorite_tools",
    indices = [Index(value = ["createdAt"])]
)
data class FavoriteToolEntity(
    @PrimaryKey val id: String,
    val title: String,
    val content: String,
    val type: String,
    val sourceMessageId: String?,
    val createdAt: Long
) {
    companion object {
        const val TYPE_HTML = "html"
        const val TYPE_MARKDOWN = "markdown"
    }
}
