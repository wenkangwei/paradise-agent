package com.example.aichat.data.local.mapper

import com.example.aichat.data.local.entity.ConversationEntity
import com.example.aichat.data.local.entity.MessageEntity
import com.example.aichat.domain.model.Attachment
import com.example.aichat.domain.model.Conversation
import com.example.aichat.domain.model.Message
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
    attachments = parseAttachments(attachmentsJson)
)

fun Message.toEntity(): MessageEntity = MessageEntity(
    id = id,
    conversationId = conversationId,
    role = role.name,
    content = content,
    timestamp = timestamp,
    attachmentsJson = gson.toJson(attachments)
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
    return runCatching { gson.fromJson(json, type) }.getOrDefault(emptyList())
}
