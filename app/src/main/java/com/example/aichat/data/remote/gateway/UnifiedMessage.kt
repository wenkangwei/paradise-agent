package com.example.aichat.data.remote.gateway

import com.google.gson.annotations.JsonAdapter
import com.google.gson.annotations.SerializedName

/**
 * # 统一消息格式（见 docs/API_GATEWAY_SPEC.md §2）
 *
 * parts-based message：一次对话消息的 `content` 一律数组化，每个 part 带 `type`
 * （text / image / audio / file / tool_result）。这样所有模态（文本、图片、
 * 语音、附件）都共用同一份消息结构，不再像 OpenAI/Anthropic 那样按模态拆字段。
 *
 * **现状**：v3 的 [com.example.aichat.data.remote.dto.MessageDto] 仍按 OpenAI
 * 格式（image_url）走；网关协议 v1.1 落地后，ChatRemoteDataSource 会切换到
 * 本数据类 + [GatewayClient]。
 *
 * 兼容性：本文件只定义数据类 + Gson adapter，不影响现有代码路径。
 */

/**
 * 一条消息（user / assistant / system / tool）。
 *
 * 与 OpenAI 的差别：
 * - `content` 永远是 `List<UnifiedPart>`（OpenAI 允许 string 或数组）
 * - 增加了 `name`、`tool_call_id`、`tool_calls` 字段以覆盖工具场景
 */
data class UnifiedMessage(
    @SerializedName("role") val role: String,
    @SerializedName("content") val content: List<UnifiedPart>,
    @SerializedName("name") val name: String? = null,
    @SerializedName("tool_call_id") val toolCallId: String? = null,
    @SerializedName("tool_calls") val toolCalls: List<UnifiedToolCall>? = null
) {
    companion object {
        fun text(role: String, text: String): UnifiedMessage = UnifiedMessage(
            role = role,
            content = listOf(UnifiedPart.Text(text = text))
        )
    }
}

/**
 * 单个 content part。用 `@JsonAdapter` 实现多态序列化。
 */
@JsonAdapter(UnifiedPartAdapter::class)
sealed class UnifiedPart {

    @SerializedName("type")
    val type: String = javaClass.simpleName.lowercase()

    data class Text(
        @SerializedName("text") val text: String
    ) : UnifiedPart()

    data class Image(
        @SerializedName("image_url") val imageUrl: String,
        @SerializedName("detail") val detail: String? = null
    ) : UnifiedPart()

    data class Audio(
        @SerializedName("audio_url") val audioUrl: String,
        @SerializedName("format") val format: String? = null,
        @SerializedName("sample_rate") val sampleRate: Int? = null,
        @SerializedName("language") val language: String? = null
    ) : UnifiedPart()

    data class File(
        @SerializedName("file_url") val fileUrl: String,
        @SerializedName("filename") val filename: String? = null,
        @SerializedName("mime_type") val mimeType: String? = null
    ) : UnifiedPart()

    data class ToolResult(
        @SerializedName("tool_call_id") val toolCallId: String,
        @SerializedName("output") val output: String
    ) : UnifiedPart()
}

/** 工具调用（assistant 发起） */
data class UnifiedToolCall(
    @SerializedName("id") val id: String,
    @SerializedName("type") val type: String = "function",
    @SerializedName("function") val function: UnifiedToolFunction
)

data class UnifiedToolFunction(
    @SerializedName("name") val name: String,
    @SerializedName("arguments") val arguments: String
)

/**
 * Gson 多态 adapter：序列化时根据子类写入 `type` 字段。
 */
private class UnifiedPartAdapter : com.google.gson.JsonSerializer<UnifiedPart>, com.google.gson.JsonDeserializer<UnifiedPart> {
    override fun serialize(
        src: UnifiedPart,
        typeOfSrc: java.lang.reflect.Type,
        context: com.google.gson.JsonSerializationContext
    ): com.google.gson.JsonElement {
        val obj = com.google.gson.JsonObject()
        when (src) {
            is UnifiedPart.Text -> {
                obj.addProperty("type", "text")
                obj.addProperty("text", src.text)
            }
            is UnifiedPart.Image -> {
                obj.addProperty("type", "image")
                obj.addProperty("image_url", src.imageUrl)
                src.detail?.let { obj.addProperty("detail", it) }
            }
            is UnifiedPart.Audio -> {
                obj.addProperty("type", "audio")
                obj.addProperty("audio_url", src.audioUrl)
                src.format?.let { obj.addProperty("format", it) }
                src.sampleRate?.let { obj.addProperty("sample_rate", it) }
                src.language?.let { obj.addProperty("language", it) }
            }
            is UnifiedPart.File -> {
                obj.addProperty("type", "file")
                obj.addProperty("file_url", src.fileUrl)
                src.filename?.let { obj.addProperty("filename", it) }
                src.mimeType?.let { obj.addProperty("mime_type", it) }
            }
            is UnifiedPart.ToolResult -> {
                obj.addProperty("type", "tool_result")
                obj.addProperty("tool_call_id", src.toolCallId)
                obj.addProperty("output", src.output)
            }
        }
        return obj
    }

    override fun deserialize(
        json: com.google.gson.JsonElement,
        typeOfT: java.lang.reflect.Type,
        context: com.google.gson.JsonDeserializationContext
    ): UnifiedPart {
        val obj = json.asJsonObject
        return when (obj.get("type")?.asString) {
            "image" -> UnifiedPart.Image(
                imageUrl = obj.get("image_url")?.asString ?: "",
                detail = obj.get("detail")?.asString
            )
            "audio" -> UnifiedPart.Audio(
                audioUrl = obj.get("audio_url")?.asString ?: "",
                format = obj.get("format")?.asString,
                sampleRate = obj.get("sample_rate")?.takeIf { !it.isJsonNull }?.asInt,
                language = obj.get("language")?.asString
            )
            "file" -> UnifiedPart.File(
                fileUrl = obj.get("file_url")?.asString ?: "",
                filename = obj.get("filename")?.asString,
                mimeType = obj.get("mime_type")?.asString
            )
            "tool_result" -> UnifiedPart.ToolResult(
                toolCallId = obj.get("tool_call_id")?.asString ?: "",
                output = obj.get("output")?.asString ?: ""
            )
            else -> UnifiedPart.Text(
                text = obj.get("text")?.asString ?: ""
            )
        }
    }
}
