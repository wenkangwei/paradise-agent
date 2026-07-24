package com.example.aichat.data.local.mapper

import com.example.aichat.data.local.entity.ConversationEntity
import com.example.aichat.data.local.entity.MessageEntity
import com.example.aichat.domain.model.Attachment
import com.example.aichat.domain.model.Conversation
import com.example.aichat.domain.model.Message
import com.example.aichat.domain.model.MessageMetadata
import com.example.aichat.domain.model.MessageStatus
import com.example.aichat.domain.model.Role
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken

private val gson = Gson()

fun ConversationEntity.toDomain(): Conversation = Conversation(
    id = id,
    title = title,
    createdAt = createdAt,
    updatedAt = updatedAt,
    lastMessage = lastMessage
)

fun MessageEntity.toDomain(): Message = Message(
    id = id,
    conversationId = conversationId,
    role = runCatching { Role.valueOf(role) }.getOrDefault(Role.SYSTEM),
    content = content,
    timestamp = timestamp,
    attachments = parseAttachments(attachmentsJson),
    status = runCatching { MessageStatus.valueOf(status) }.getOrDefault(MessageStatus.COMPLETE),
    reasoningContent = reasoningContent,
    metadata = metadataJson?.let(::parseMetadata)
)

fun Message.toEntity(): MessageEntity = MessageEntity(
    id = id,
    conversationId = conversationId,
    role = role.name,
    content = content,
    timestamp = timestamp,
    attachmentsJson = gson.toJson(attachments),
    status = status.name,
    reasoningContent = reasoningContent,
    metadataJson = metadata?.let { gson.toJson(it) }
)

fun Conversation.toEntity(): ConversationEntity = ConversationEntity(
    id = id,
    title = title,
    createdAt = createdAt,
    updatedAt = updatedAt,
    lastMessage = lastMessage
)

private fun parseAttachments(json: String): List<Attachment> {
    if (json.isBlank()) return emptyList()
    val type = object : TypeToken<List<Attachment>>() {}.type
    return runCatching {
        gson.fromJson<List<Attachment>>(json, type) ?: emptyList()
    }.getOrDefault(emptyList())
}

private fun parseMetadata(json: String): MessageMetadata? =
    runCatching { gson.fromJson(json, MessageMetadata::class.java) }.getOrNull()
