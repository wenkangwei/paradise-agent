package com.example.aichat.data.remote

import com.example.aichat.data.remote.dto.ChatStreamChunkDto
import com.google.gson.Gson
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn
import okhttp3.ResponseBody

/**
 * Parses a streaming SSE response from the chat completions endpoint.
 *
 * Reads the [ResponseBody] line by line, extracts JSON payloads from `data:` lines,
 * parses them as [ChatStreamChunkDto], and emits each non-null content delta.
 *
 * The returned Flow is cancellable: cancelling collection closes the response body
 * and the underlying connection.
 */
class AiStreamClient {

    private val gson = Gson()

    /**
     * Converts a streaming [ResponseBody] into a Flow of content token strings.
     *
     * Uses [flow] builder which supports cancellation at each `emit` suspension point.
     * When the collector cancels (e.g., via `collectLatest` in ViewModel), the coroutine
     * is cancelled and the `finally` block closes the response body.
     */
    fun toTokenFlow(responseBody: ResponseBody): Flow<String> = flow {
        try {
            val source = responseBody.source()
            while (true) {
                val line = source.readUtf8Line() ?: break

                // Skip empty lines (SSE event delimiters)
                if (line.isBlank()) continue

                // Only process data lines
                if (!line.startsWith("data:", ignoreCase = true)) continue

                val payload = line.removePrefix("data:").trim()

                // [DONE] signals end of stream
                if (payload == "[DONE]") break

                // Parse JSON chunk and extract content delta
                val chunk = runCatching {
                    gson.fromJson(payload, ChatStreamChunkDto::class.java)
                }.getOrNull()

                val delta = chunk?.contentDelta
                if (!delta.isNullOrEmpty()) {
                    emit(delta)
                }
            }
        } finally {
            runCatching { responseBody.close() }
        }
    }.flowOn(Dispatchers.IO)
}
