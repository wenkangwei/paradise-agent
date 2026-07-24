package com.example.aichat.data.remote

import com.example.aichat.data.provider.StreamEvent
import com.example.aichat.data.remote.dto.ChatStreamChunkDto
import com.google.gson.Gson
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn
import okhttp3.ResponseBody
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Parses a streaming SSE response from the chat completions endpoint.
 *
 * Reads the [ResponseBody] line by line, extracts JSON payloads from `data:` lines,
 * parses them as [ChatStreamChunkDto], and emits structured [StreamEvent]s:
 *  - [StreamEvent.ContentDelta] for normal token deltas
 *  - [StreamEvent.ReasoningDelta] for `reasoning_content` (DeepSeek-R1, Qwen3, ...)
 *  - [StreamEvent.Finish] when `finish_reason` arrives or `[DONE]` is seen
 *
 * Cancellation semantics: when the collector cancels, the coroutine is cancelled
 * and the `finally` block closes the response body. The emitted [StreamEvent.Cancelled]
 * is *not* sent here — the caller (ChatRemoteDataSource / LlmProvider) decides whether
 * to emit it based on whether any content was accumulated before cancellation.
 */
@Singleton
class AiStreamClient @Inject constructor() {

    private val gson = Gson()

    /**
     * Converts a streaming [ResponseBody] into a [Flow] of [StreamEvent]s.
     *
     * Replaces the old `toTokenFlow(): Flow<String>` — callers now discriminate
     * between content, reasoning and finish events instead of receiving raw tokens.
     */
    fun toEventFlow(responseBody: ResponseBody): Flow<StreamEvent> = flow {
        try {
            val source = responseBody.source()
            var finishEmitted = false
            while (true) {
                val line = source.readUtf8Line() ?: break

                if (line.isBlank()) continue
                if (!line.startsWith("data:", ignoreCase = true)) continue

                val payload = line.removePrefix("data:").trim()
                if (payload == "[DONE]") {
                    if (!finishEmitted) emit(StreamEvent.Finish(reason = null))
                    break
                }

                val chunk = runCatching {
                    gson.fromJson(payload, ChatStreamChunkDto::class.java)
                }.getOrNull() ?: continue

                chunk.reasoningDelta?.takeIf { it.isNotEmpty() }?.let {
                    emit(StreamEvent.ReasoningDelta(text = it))
                }
                chunk.contentDelta?.takeIf { it.isNotEmpty() }?.let {
                    emit(StreamEvent.ContentDelta(text = it))
                }
                chunk.finishReason?.let { reason ->
                    finishEmitted = true
                    emit(StreamEvent.Finish(reason = reason))
                }
            }
        } finally {
            runCatching { responseBody.close() }
        }
    }.flowOn(Dispatchers.IO)
}
