package com.example.aichat.data.remote.gateway

import com.google.gson.annotations.SerializedName

/**
 * # 网关 API 请求 / 响应 DTO（见 docs/API_GATEWAY_SPEC.md）
 *
 * 覆盖 7 个端点：
 *  - `/v1/chat/completions`   [UnifiedChatRequest]
 *  - `/v1/asr`                [AsrRequest] / [AsrResponse]
 *  - `/v1/tts`                [TtsRequest]
 *  - `/v1/images/generations` [ImageGenRequest]
 *  - `/v1/images/edits`       [ImageEditRequest]
 *  - `/v1/agent/run`          [AgentRunRequest]
 *  - `/v1/files`              [FileUploadResponse]
 *  - `/v1/models`             [ModelsResponse]
 *
 * **当前状态（v3）**：DTO 已定义；[GatewayClient] 接口已声明；尚未实际调用。
 * 等后端服务起来后，把 [com.example.aichat.data.remote.ChatRemoteDataSource]
 * 切到 [GatewayClient.chatStream]。
 */

// =================== /v1/chat/completions ===================

data class UnifiedChatRequest(
    @SerializedName("model") val model: String,
    @SerializedName("messages") val messages: List<UnifiedMessage>,
    @SerializedName("stream") val stream: Boolean = true,
    @SerializedName("temperature") val temperature: Float? = null,
    @SerializedName("max_tokens") val maxTokens: Int? = null,
    @SerializedName("top_p") val topP: Float? = null,
    @SerializedName("enable_reasoning") val enableReasoning: Boolean? = null,
    @SerializedName("enable_rag") val enableRag: Boolean? = null,
    @SerializedName("rag_collection") val ragCollection: String? = null,
    @SerializedName("agent_id") val agentId: String? = null,
    @SerializedName("tools") val tools: List<Map<String, Any>>? = null,
    @SerializedName("tool_choice") val toolChoice: String? = null
)

// =================== /v1/asr ===================

data class AsrRequest(
    @SerializedName("audio_url") val audioUrl: String,
    @SerializedName("format") val format: String? = null,
    @SerializedName("model") val model: String = "whisper-large-v3",
    @SerializedName("language") val language: String? = null,
    @SerializedName("prompt") val prompt: String? = null,
    @SerializedName("response_format") val responseFormat: String = "json"
)

data class AsrResponse(
    @SerializedName("text") val text: String,
    @SerializedName("language") val language: String? = null,
    @SerializedName("duration") val duration: Float? = null,
    @SerializedName("segments") val segments: List<AsrSegment>? = null
)

data class AsrSegment(
    @SerializedName("start") val start: Float,
    @SerializedName("end") val end: Float,
    @SerializedName("text") val text: String
)

// =================== /v1/tts ===================

data class TtsRequest(
    @SerializedName("model") val model: String = "doubao-tts",
    @SerializedName("input") val input: String,
    @SerializedName("voice") val voice: String,
    @SerializedName("response_format") val responseFormat: String = "mp3",
    @SerializedName("speed") val speed: Float = 1.0f,
    @SerializedName("stream") val stream: Boolean = false
)

// =================== /v1/images/generations ===================

data class ImageGenRequest(
    @SerializedName("model") val model: String,
    @SerializedName("prompt") val prompt: String,
    @SerializedName("negative_prompt") val negativePrompt: String? = null,
    @SerializedName("n") val n: Int = 1,
    @SerializedName("size") val size: String = "1024x1024",
    @SerializedName("quality") val quality: String = "standard",
    @SerializedName("style") val style: String? = null,
    @SerializedName("response_format") val responseFormat: String = "url"
)

data class ImageEditRequest(
    @SerializedName("model") val model: String,
    @SerializedName("prompt") val prompt: String,
    @SerializedName("image") val image: String,
    @SerializedName("mask") val mask: String? = null,
    @SerializedName("strength") val strength: Float = 0.7f,
    @SerializedName("n") val n: Int = 1,
    @SerializedName("size") val size: String = "1024x1024",
    @SerializedName("response_format") val responseFormat: String = "url"
)

data class ImageResponse(
    @SerializedName("created") val created: Long,
    @SerializedName("data") val data: List<ImageData>
)

data class ImageData(
    @SerializedName("url") val url: String? = null,
    @SerializedName("b64_json") val b64Json: String? = null,
    @SerializedName("revised_prompt") val revisedPrompt: String? = null
)

// =================== /v1/agent/run ===================

data class AgentRunRequest(
    @SerializedName("agent_id") val agentId: String,
    @SerializedName("messages") val messages: List<UnifiedMessage>,
    @SerializedName("max_iterations") val maxIterations: Int = 10,
    @SerializedName("stream") val stream: Boolean = true,
    @SerializedName("tools") val tools: List<Map<String, Any>>? = null,
    @SerializedName("tool_choice") val toolChoice: String = "auto"
)

// =================== /v1/files ===================

data class FileUploadResponse(
    @SerializedName("id") val id: String,
    @SerializedName("url") val url: String,
    @SerializedName("filename") val filename: String,
    @SerializedName("mime_type") val mimeType: String,
    @SerializedName("size") val size: Long,
    @SerializedName("expires_at") val expiresAt: Long? = null
)

// =================== /v1/models ===================

data class ModelsResponse(
    @SerializedName("object") val obj: String = "list",
    @SerializedName("data") val data: List<ModelInfo>
)

data class ModelInfo(
    @SerializedName("id") val id: String,
    @SerializedName("object") val obj: String = "model",
    @SerializedName("category") val category: String,           // chat / asr / tts / t2i / i2i / embedding
    @SerializedName("capabilities") val capabilities: List<String> = emptyList(),
    @SerializedName("context_window") val contextWindow: Int? = null,
    @SerializedName("owned_by") val ownedBy: String? = null
)
