package com.example.aichat.data.remote.dto

import com.google.gson.JsonDeserializationContext
import com.google.gson.JsonDeserializer
import com.google.gson.JsonElement
import com.google.gson.JsonObject
import com.google.gson.JsonSerializationContext
import com.google.gson.JsonSerializer
import com.google.gson.annotations.JsonAdapter
import com.google.gson.annotations.SerializedName

/**
 * OpenAI multimodal content part. A message's content can be an array of typed parts.
 *
 * Serialized as polymorphic JSON objects with a "type" discriminator:
 * - { "type": "text", "text": "..." }
 * - { "type": "image_url", "image_url": { "url": "..." } }
 */
@JsonAdapter(ContentPartAdapter::class)
sealed class ContentPart {

    data class Text(
        @SerializedName("text") val text: String
    ) : ContentPart()

    data class ImageUrl(
        @SerializedName("image_url") val imageUrl: ImageUrlData
    ) : ContentPart()
}

data class ImageUrlData(
    @SerializedName("url") val url: String,
    @SerializedName("detail") val detail: String? = null
)

/**
 * Represents a message in the OpenAI chat completions API.
 * Content is always a list of typed parts (text or image_url).
 */
data class MessageDto(
    @SerializedName("role") val role: String,
    @SerializedName("content") val content: List<ContentPart>
)

/**
 * Convenience factory: builds a text-only MessageDto (single Text part).
 */
fun MessageDto(role: String, content: String): MessageDto =
    MessageDto(role = role, content = listOf(ContentPart.Text(text = content)))

/**
 * Custom Gson adapter for [ContentPart] that handles polymorphic serialization/deserialization
 * based on the "type" field.
 */
private class ContentPartAdapter : JsonSerializer<ContentPart>, JsonDeserializer<ContentPart> {

    override fun serialize(
        src: ContentPart,
        typeOfSrc: java.lang.reflect.Type,
        context: JsonSerializationContext
    ): JsonElement = when (src) {
        is ContentPart.Text -> JsonObject().apply {
            addProperty("type", "text")
            addProperty("text", src.text)
        }
        is ContentPart.ImageUrl -> JsonObject().apply {
            addProperty("type", "image_url")
            add("image_url", context.serialize(src.imageUrl))
        }
    }

    override fun deserialize(
        json: JsonElement,
        typeOfT: java.lang.reflect.Type,
        context: JsonDeserializationContext
    ): ContentPart {
        val obj = json.asJsonObject
        return when (obj.get("type")?.asString) {
            "image_url" -> ContentPart.ImageUrl(
                imageUrl = context.deserialize(obj.get("image_url"), ImageUrlData::class.java)
            )
            else -> ContentPart.Text(text = obj.get("text")?.asString ?: "")
        }
    }
}
