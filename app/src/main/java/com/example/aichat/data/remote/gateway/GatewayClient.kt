package com.example.aichat.data.remote.gateway

import okhttp3.MultipartBody
import okhttp3.RequestBody
import okhttp3.ResponseBody
import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.Header
import retrofit2.http.Multipart
import retrofit2.http.POST
import retrofit2.http.Part

/**
 * # 统一模型网关 Retrofit 接口（见 docs/API_GATEWAY_SPEC.md §3）
 *
 * 客户端**只对接这份接口**，所有上游模型差异（OpenAI / Anthropic / GLM / 自家）
 * 都由网关在后端抹平。
 *
 * ## 当前状态（v3）
 * - DTO 已定义在同包下（[UnifiedMessage] / [UnifiedChatRequest] / …）
 * - Retrofit 接口已声明，但 **尚未注入** 到 Hilt graph；ChatRemoteDataSource
 *   仍走老的 OpenAI 兼容路径（`AiApiService`）
 * - 网关后端服务搭建后，只需：
 *   1. 在 `NetworkModule` 里 provide `GatewayClient`
 *   2. 在 `ChatRemoteDataSource` 里把 `chatStream` 切到 `GatewayClient.chatStream`
 *   3. ASR/TTS/T2I/I2I 按需在 UI 层接入
 *
 * ## BaseUrl
 * 由 `ApiProfileEntity.baseUrl` 决定，与现有 Supplier 体系一致 ——
 * 当用户选 `MyGatewaySupplier`（见 BuiltinSuppliers.kt）时，baseUrl 应指向
 * 网关生产/开发地址。
 */
interface GatewayClient {

    // =================== LLM 对话 ===================

    /**
     * 流式 LLM 对话。返回原始 ResponseBody（SSE 流），由调用方按
     * [com.example.aichat.data.remote.AiStreamClient] 同样的方式逐行解析。
     *
     * SSE 事件类型见 docs/API_GATEWAY_SPEC.md §4.2：
     *   text_delta / reasoning_delta / audio_delta / image /
     *   search_results / tool_call / tool_result / usage / finish / error
     */
    @POST("chat/completions")
    suspend fun chatStream(
        @Body req: UnifiedChatRequest,
        @Header("Authorization") auth: String
    ): Response<ResponseBody>

    // =================== 语音识别 (ASR) ===================

    /** 短音频（<25MB）语音转文字。 */
    @POST("asr")
    suspend fun asr(
        @Body req: AsrRequest,
        @Header("Authorization") auth: String
    ): AsrResponse

    // =================== 语音合成 (TTS) ===================

    /**
     * 文字转语音。stream=false 直接返回二进制 audio/mpeg；stream=true 返回
     * SSE 流（audio_delta 事件，base64 分块）。
     */
    @POST("tts")
    suspend fun tts(
        @Body req: TtsRequest,
        @Header("Authorization") auth: String
    ): Response<ResponseBody>

    // =================== 文生图 / 图生图 ===================

    /** 文生图（doubao-t2i / kolors / cogview-3 / dall-e-3 …）。 */
    @POST("images/generations")
    suspend fun generateImage(
        @Body req: ImageGenRequest,
        @Header("Authorization") auth: String
    ): ImageResponse

    /** 图生图 / 图片编辑（需要蒙版）。 */
    @POST("images/edits")
    suspend fun editImage(
        @Body req: ImageEditRequest,
        @Header("Authorization") auth: String
    ): ImageResponse

    // =================== Agent 执行 ===================

    /**
     * Agent 编排（多轮 LLM + 工具调用）。事件同 chatStream，额外可能
     * 携带 tool_call / tool_result 事件。
     */
    @POST("agent/run")
    suspend fun agentRun(
        @Body req: AgentRunRequest,
        @Header("Authorization") auth: String
    ): Response<ResponseBody>

    // =================== 文件上传 ===================

    /**
     * 大文件上传到网关对象存储。返回的 URL 可直接填到 [UnifiedPart.File]
     * 的 file_url 字段，避免 base64 撑爆请求体。
     */
    @Multipart
    @POST("files")
    suspend fun uploadFile(
        @Part file: MultipartBody.Part,
        @Part("purpose") purpose: RequestBody,
        @Header("Authorization") auth: String
    ): FileUploadResponse

    // =================== 模型列表 ===================

    /** 列出网关支持的所有模型（按 category 分类）。 */
    @GET("models")
    suspend fun listModels(
        @Header("Authorization") auth: String
    ): ModelsResponse
}
