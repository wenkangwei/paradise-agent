package com.example.aichat.data.repository

import com.example.aichat.data.remote.DataCollectionApi
import com.example.aichat.data.remote.dto.ProactiveMessageDto
import com.example.aichat.data.remote.dto.ProactivePollResponse
import com.example.aichat.data.remote.dto.ProactiveStatusResponse
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Repository for the proactive agent system.
 *
 * Handles:
 *   - Long-polling for proactive messages from the server
 *   - Registering conversations for proactive checks
 *   - Notifying user activity (reset cooldown)
 */
@Singleton
class ProactiveRepository @Inject constructor(
    private val api: DataCollectionApi,
    private val dataUploadManager: DataUploadManager
) {
    private val baseUrl get() = dataUploadManager.getServerUrl()

    /**
     * Long-poll for the next proactive message.
     * Blocks up to 30 seconds, returns the message or null.
     */
    suspend fun poll(convId: String): ProactiveMessageDto? = withContext(Dispatchers.IO) {
        try {
            val response = api.proactivePoll("$baseUrl/api/agent/proactive/poll", convId)
            if (response.isSuccessful) {
                response.body()?.message
            } else {
                null
            }
        } catch (_: Exception) {
            null
        }
    }

    /**
     * Register a conversation for periodic proactive checks.
     */
    suspend fun register(convId: String): Boolean = withContext(Dispatchers.IO) {
        try {
            api.proactiveRegister(
                "$baseUrl/api/agent/proactive/register",
                DataCollectionApi.RegisterRequest(convId)
            ).isSuccessful
        } catch (_: Exception) {
            false
        }
    }

    /**
     * Notify that user sent a message — resets the proactive cooldown.
     */
    suspend fun notifyActivity(convId: String) = withContext(Dispatchers.IO) {
        try {
            api.proactiveActivity(
                "$baseUrl/api/agent/proactive/activity",
                DataCollectionApi.RegisterRequest(convId)
            )
        } catch (_: Exception) {
            // Silent failure — this is best-effort
        }
    }

    /**
     * Get proactive scheduler status from the server.
     */
    suspend fun getStatus(): ProactiveStatusResponse? = withContext(Dispatchers.IO) {
        try {
            val response = api.proactiveStatus("$baseUrl/api/agent/proactive/status")
            if (response.isSuccessful) response.body() else null
        } catch (_: Exception) {
            null
        }
    }
}
