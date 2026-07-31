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
 *       "delta": {
 *         "content": "Hello",
 *         "reasoning_content": "Thinking..."
 *       },
 *       "index": 0,
 *       "finish_reason": null
 *     }
 *   ]
 * }
 * ```
 *
 * `reasoning_content` is emitted by DeepSeek-R1, Qwen3 thinking mode, and
 * several other models that expose chain-of-thought alongside the answer.
 */
data class ChatStreamChunkDto(
    @SerializedName("choices") val choices: List<ChoiceDto> = emptyList()
) {
    /** First-choice content delta, or null if absent. */
    val contentDelta: String?
        get() = choices.firstOrNull()?.delta?.content

    /** First-choice reasoning delta (e.g. DeepSeek-R1 `reasoning_content`). */
    val reasoningDelta: String?
        get() = choices.firstOrNull()?.delta?.reasoningContent

    /** First-choice finish_reason; non-null on the terminal chunk. */
    val finishReason: String?
        get() = choices.firstOrNull()?.finishReason

    /** Tool cards (search results, etc.) from agent tool calls. */
    val toolCards: List<ToolCardDto>?
        get() = choices.firstOrNull()?.delta?.toolCards
}

data class ChoiceDto(
    @SerializedName("index") val index: Int = 0,
    @SerializedName("delta") val delta: DeltaDto? = null,
    @SerializedName("finish_reason") val finishReason: String? = null
)

data class DeltaDto(
    @SerializedName("role") val role: String? = null,
    @SerializedName("content") val content: String? = null,
    @SerializedName("reasoning_content") val reasoningContent: String? = null,
    @SerializedName("tool_cards") val toolCards: List<ToolCardDto>? = null
)

data class ToolCardDto(
    @SerializedName("type") val type: String = "",
    @SerializedName("title") val title: String = "",
    @SerializedName("results") val results: List<SearchResultDto>? = null
)

data class SearchResultDto(
    @SerializedName("title") val title: String = "",
    @SerializedName("url") val url: String = "",
    @SerializedName("snippet") val snippet: String = ""
)
