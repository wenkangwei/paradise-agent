package com.example.aichat.data.remote.dto

import com.google.gson.annotations.SerializedName

data class ChatRequestDto(
    @SerializedName("model") val model: String,
    @SerializedName("messages") val messages: List<MessageDto>,
    @SerializedName("stream") val stream: Boolean = true,
    @SerializedName("temperature") val temperature: Double? = null
)
