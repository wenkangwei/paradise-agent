package com.example.aichat.ui.chat.model

import android.net.Uri
import com.example.aichat.domain.model.Message
import com.example.aichat.domain.model.Role as DomainRole

/**
 * Maps a domain-layer [Message] to a UI-layer [ChatMessage].
 *
 * Domain attachments (uri = data URL or http URL) are mapped to UI attachments
 * using the same uri for Coil's AsyncImage rendering.
 */
fun Message.toChatMessage(): ChatMessage {
    return ChatMessage(
        id = id,
        role = when (role) {
            DomainRole.USER -> Role.USER
            DomainRole.ASSISTANT -> Role.ASSISTANT
            DomainRole.SYSTEM -> Role.SYSTEM
        },
        content = content,
        isStreaming = false,
        attachments = attachments.map { it.toUiAttachment() },
        reasoningContent = reasoningContent,
        metadata = metadata
    )
}

/**
 * Maps a list of domain [Message]s to UI [ChatMessage]s.
 */
fun List<Message>.toChatMessages(): List<ChatMessage> {
    return map { it.toChatMessage() }
}

/**
 * Maps a UI-layer [Attachment] to a domain-layer [com.example.aichat.domain.model.Attachment].
 * Used when persisting user messages with attachments.
 *
 * Note: [Attachment.uri] at the UI layer is typically a content:// URI.
 * It should be encoded to a data URL via AttachmentEncoder before this mapping.
 */
fun Attachment.toDomainAttachment(): com.example.aichat.domain.model.Attachment {
    return com.example.aichat.domain.model.Attachment(
        id = id,
        mimeType = mimeType,
        uri = uri
    )
}

/**
 * Maps a domain-layer [com.example.aichat.domain.model.Attachment] to a UI-layer [Attachment].
 * The domain uri (data URL or http URL) is used directly for Coil rendering.
 */
private fun com.example.aichat.domain.model.Attachment.toUiAttachment(): Attachment {
    val displayName = runCatching {
        Uri.parse(uri).lastPathSegment
    }.getOrNull() ?: ""
    return Attachment(
        id = id,
        uri = uri,
        mimeType = mimeType,
        displayName = displayName
    )
}
