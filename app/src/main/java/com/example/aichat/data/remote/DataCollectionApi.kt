package com.example.aichat.data.remote

import com.example.aichat.data.remote.dto.UploadRequestDto
import com.example.aichat.data.remote.dto.ProactivePollResponse
import com.example.aichat.data.remote.dto.ProactiveStatusResponse
import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.Query
import retrofit2.http.Url

/**
 * Retrofit interface for the AiChat agent server's data collection and
 * proactive endpoints. Uses @Url for all calls so the base server address
 * can be changed at runtime (same pattern as [AiApiService]).
 */
interface DataCollectionApi {

    // ── Data Upload ──────────────────────────────────────────────

    @POST
    suspend fun uploadData(@Url url: String, @Body body: UploadRequestDto): Response<Unit>

    // ── Proactive Agent ──────────────────────────────────────────

    @GET
    suspend fun proactivePoll(
        @Url url: String,
        @Query("conv_id") convId: String
    ): Response<ProactivePollResponse>

    @POST
    suspend fun proactiveRegister(@Url url: String, @Body body: RegisterRequest): Response<Unit>

    @POST
    suspend fun proactiveActivity(@Url url: String, @Body body: RegisterRequest): Response<Unit>

    @GET
    suspend fun proactiveStatus(@Url url: String): Response<ProactiveStatusResponse>

    data class RegisterRequest(val conv_id: String)
}
