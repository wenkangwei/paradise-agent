package com.example.aichat.data.remote

import com.example.aichat.data.remote.dto.ChatRequestDto
import okhttp3.ResponseBody
import retrofit2.http.Body
import retrofit2.http.Headers
import retrofit2.http.POST
import retrofit2.http.Streaming

interface AiApiService {

    @Streaming
    @Headers("Accept: text/event-stream")
    @POST("v1/chat/completions")
    suspend fun streamChat(@Body request: ChatRequestDto): ResponseBody
}
