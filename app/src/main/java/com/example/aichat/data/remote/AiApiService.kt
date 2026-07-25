package com.example.aichat.data.remote

import com.example.aichat.data.remote.dto.ChatRequestDto
import okhttp3.ResponseBody
import retrofit2.http.Body
import retrofit2.http.Headers
import retrofit2.http.POST
import retrofit2.http.Streaming
import retrofit2.http.Url

interface AiApiService {

    /**
     * Streams a chat completion. The endpoint is supplied by the caller via
     * [url] so that profiles in `fullUrlMode` (where baseUrl is already the
     * complete chat-completions URL) and legacy profiles (where baseUrl only
     * carries the version segment and `/chat/completions` must be appended)
     * can share the same service interface.
     *
     * - Legacy mode: callers pass the relative path `chat/completions`,
     *   resolved against the Retrofit baseUrl.
     * - Full-URL mode: callers pass an absolute URL (`http(s)://...`); the
     *   Retrofit baseUrl is ignored for that call.
     */
    @Streaming
    @Headers("Accept: text/event-stream")
    @POST
    suspend fun streamChat(@Url url: String, @Body request: ChatRequestDto): ResponseBody
}
