package com.example.aichat.service

import android.content.Context
import androidx.hilt.work.HiltWorker
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.example.aichat.data.local.LastConversationTracker
import com.example.aichat.data.repository.ProactiveRepository
import com.example.aichat.domain.repository.ChatRepository
import com.example.aichat.domain.model.Message
import com.example.aichat.domain.model.MessageStatus
import com.example.aichat.domain.model.Role
import dagger.assisted.Assisted
import dagger.assisted.AssistedInject
import java.util.concurrent.TimeUnit

/**
 * Periodic worker that polls the server for proactive messages.
 *
 * Runs every 2 minutes. When a proactive message is received, it's inserted
 * into the conversation as an AI message — the UI auto-refreshes via Room
 * Flow observation.
 */
@HiltWorker
class ProactivePollingWorker @AssistedInject constructor(
    @Assisted context: Context,
    @Assisted params: WorkerParameters,
    private val proactiveRepo: ProactiveRepository,
    private val repository: ChatRepository,
    private val lastConversationTracker: LastConversationTracker
) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val convId = lastConversationTracker.load() ?: return Result.success()

        return try {
            val message = proactiveRepo.poll(convId)
            if (message != null) {
                // Insert proactive message into the conversation
                repository.appendMessage(
                    Message(
                        id = "proactive_${System.currentTimeMillis()}",
                        conversationId = convId,
                        role = Role.ASSISTANT,
                        content = message.content,
                        timestamp = System.currentTimeMillis(),
                        status = MessageStatus.COMPLETE,
                        reasoningContent = "💡 主动消息: ${message.reason}"
                    )
                )
            }
            Result.success()
        } catch (_: Exception) {
            Result.retry()
        }
    }

    companion object {
        const val WORK_NAME = "proactive_polling"

        fun buildRequest() = androidx.work.PeriodicWorkRequestBuilder<ProactivePollingWorker>(
            2, TimeUnit.MINUTES
        ).build()
    }
}
