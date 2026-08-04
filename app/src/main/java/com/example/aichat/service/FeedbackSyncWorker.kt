package com.example.aichat.service

import android.content.Context
import androidx.hilt.work.HiltWorker
import androidx.work.CoroutineWorker
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkerParameters
import com.example.aichat.data.repository.DataUploadManager
import dagger.assisted.Assisted
import dagger.assisted.AssistedInject
import java.util.concurrent.TimeUnit

/**
 * Periodic worker that syncs unsynced feedback data to the server.
 * Runs every 30 minutes as a fallback for event-triggered uploads.
 */
@HiltWorker
class FeedbackSyncWorker @AssistedInject constructor(
    @Assisted context: Context,
    @Assisted params: WorkerParameters,
    private val dataUploadManager: DataUploadManager
) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        return try {
            dataUploadManager.syncAllUnsynced()
            Result.success()
        } catch (_: Exception) {
            Result.retry()
        }
    }

    companion object {
        const val WORK_NAME = "feedback_sync"

        fun buildRequest() = PeriodicWorkRequestBuilder<FeedbackSyncWorker>(
            30, TimeUnit.MINUTES
        ).build()
    }
}
