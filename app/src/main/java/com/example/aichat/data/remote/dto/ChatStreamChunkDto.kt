package com.example.aichat.data.remote.dto

import com.google.gson.annotations.SerializedName

/**
 * Represents a single SSE chunk from the streaming chat completion endpoint.
 *
 * JSON structure (OpenAI-compatible):
 * ```
 * {
 *   "choices": [
 *     {
 *       "delta": { "content": "Hello" },
 *       "index": 0,
 *       "finish_reason": null
 *     }
 *   ]
 * }
 * ```
 */
data class ChatStreamChunkDto(
    @SerializedName("choices") val choices: List<ChoiceDto> = emptyList()
) {
    /**
     * Extracts the content delta from the first choice, or null if absent.
     */
    val contentDelta: String?
        get() = choices.firstOrNull()?.delta?.content
}

data class ChoiceDto(
    @SerializedName("index") val index: Int = 0,
    @SerializedName("delta") val delta: DeltaDto? = null,
    @SerializedName("finish_reason") val finishReason: String? = null
)

data class DeltaDto(
    @SerializedName("role") val role: String? = null,
    @SerializedName("content") val content: String? = null
)
