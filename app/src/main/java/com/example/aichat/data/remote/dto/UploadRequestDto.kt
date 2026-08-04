package com.example.aichat.data.remote.dto

import com.google.gson.annotations.SerializedName

/**
 * Unified upload request for the /api/data/upload endpoint.
 * See server/api/routes/data_upload.py for the server-side handler.
 */
data class UploadRequestDto(
    @SerializedName("upload_id") val uploadId: String,
    @SerializedName("data_type") val dataType: String,
    val payload: Map<String, Any>,
    val context: ContextInfo,
    @SerializedName("client_info") val clientInfo: ClientInfo
)

data class ContextInfo(
    val timestamp: Long?,
    val ip: String? = null,
    val geo: Map<String, Any>? = null,
    val locale: String? = null,
    val timezone: String? = null
)

data class ClientInfo(
    @SerializedName("user_id") val userId: String,
    @SerializedName("device_id") val deviceId: String,
    @SerializedName("app_version") val appVersion: String,
    val page: String = "chat",
    val scene: String = "default"
)

// ── Proactive responses ──────────────────────────────────────────

data class ProactivePollResponse(
    val message: ProactiveMessageDto? = null,
    val reason: String? = null
)

data class ProactiveMessageDto(
    @SerializedName("conv_id") val convId: String,
    val content: String,
    val reason: String = "",
    val timestamp: Double = 0.0,
    @SerializedName("iso_time") val isoTime: String = ""
)

data class ProactiveStatusResponse(
    val enabled: Boolean = false,
    @SerializedName("interval_seconds") val intervalSeconds: Int = 0,
    @SerializedName("cooldown_seconds") val cooldownSeconds: Int = 0,
    val model: String = "",
    @SerializedName("registered_conversations") val registeredConversations: List<String> = emptyList(),
    @SerializedName("pending_messages") val pendingMessages: Map<String, Int> = emptyMap()
)
