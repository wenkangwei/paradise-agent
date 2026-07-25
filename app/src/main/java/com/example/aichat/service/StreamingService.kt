package com.example.aichat.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import com.example.aichat.MainActivity
import com.example.aichat.domain.usecase.StreamAiReplyUseCase
import dagger.hilt.android.AndroidEntryPoint
import javax.inject.Inject
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import com.example.aichat.ui.chat.model.Attachment

private val gson = Gson()

private fun parseAttachments(json: String): List<Attachment> {
    return runCatching {
        val type = object : TypeToken<List<Attachment>>() {}.type
        gson.fromJson<List<Attachment>>(json, type) ?: emptyList()
    }.getOrDefault(emptyList())
}

/**
 * Foreground service that runs in a dedicated `:streaming` process.
 *
 * Why a separate process?
 *   On aggressive OEM ROMs (Xiaomi/Huawei/OPPO/vivo) swiping the app away or
 *   locking the screen kills the main app process. If the streaming request
 *   runs in that process, the connection dies with it. A separate process + a
 *   foreground notification gives the streaming task a much better chance of
 *   surviving until the LLM finishes.
 *
 * The service delegates the actual work to [StreamAiReplyUseCase], which reads
 * history from Room, streams the response, and writes the partial/final reply
 * back to Room. The UI observes the same Room database and updates itself.
 */
@AndroidEntryPoint
class StreamingService : Service() {

    @Inject
    lateinit var useCase: StreamAiReplyUseCase

    @Inject
    lateinit var wakeLockProvider: StreamingWakeLock

    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var streamingJob: Job? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.action ?: ACTION_START
        when (action) {
            ACTION_START -> {
                val conversationId = intent?.getStringExtra(EXTRA_CONVERSATION_ID)
                val text = intent?.getStringExtra(EXTRA_TEXT) ?: ""
                val attachmentsJson = intent?.getStringExtra(EXTRA_ATTACHMENTS_JSON) ?: "[]"
                if (conversationId == null || streamingJob?.isActive == true) {
                    stopIfIdle()
                    return START_STICKY
                }

                startForeground(NOTIFICATION_ID, buildNotification())
                wakeLockProvider.acquire(this)

                val attachments = parseAttachments(attachmentsJson)
                streamingJob = serviceScope.launch {
                    useCase(
                        conversationId = conversationId,
                        text = text,
                        attachments = attachments
                    ) { error ->
                        // Errors are written via Room/notification; the service itself
                        // does not crash the UI process.
                    }
                    // Stream finished (complete, interrupted or error) - tear down.
                    stopSelf()
                }
            }
            ACTION_STOP -> {
                streamingJob?.cancel()
                stopService()
            }
        }
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        streamingJob?.cancel()
        serviceScope.cancel()
        stopService()
        super.onDestroy()
    }

    private fun stopService() {
        wakeLockProvider.release()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun stopIfIdle() {
        if (streamingJob?.isActive != true) {
            stopService()
        }
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "AI 流式回复",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "保持后台连接，让 AI 回复在锁屏或切换应用时继续"
                setShowBadge(false)
            }
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            nm.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(): Notification {
        val intent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pendingIntent = PendingIntent.getActivity(
            this,
            0,
            intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("AI 正在回复...")
            .setContentText("保持前台运行，锁屏或切换应用不会中断")
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .setSilent(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }

    companion object {
        private const val CHANNEL_ID = "streaming_channel"
        private const val NOTIFICATION_ID = 0x5354_5245 // "STRE"
        const val ACTION_START = "com.example.aichat.action.START_STREAMING"
        const val ACTION_STOP = "com.example.aichat.action.STOP_STREAMING"
        const val EXTRA_CONVERSATION_ID = "conversation_id"
        const val EXTRA_TEXT = "text"
        const val EXTRA_ATTACHMENTS_JSON = "attachments_json"

        fun start(context: Context, conversationId: String, text: String, attachmentsJson: String) {
            val intent = Intent(context, StreamingService::class.java).apply {
                action = ACTION_START
                putExtra(EXTRA_CONVERSATION_ID, conversationId)
                putExtra(EXTRA_TEXT, text)
                putExtra(EXTRA_ATTACHMENTS_JSON, attachmentsJson)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            val intent = Intent(context, StreamingService::class.java).apply {
                action = ACTION_STOP
            }
            context.startService(intent)
        }
    }
}

/**
 * Injectable wrapper around [PowerManager.WakeLock].
 */
class StreamingWakeLock @Inject constructor() {
    private var wakeLock: PowerManager.WakeLock? = null

    @Synchronized
    fun acquire(context: Context, timeoutMs: Long = 10 * 60 * 1000L) {
        if (wakeLock?.isHeld == true) return
        runCatching {
            val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = pm.newWakeLock(
                PowerManager.PARTIAL_WAKE_LOCK,
                "aichat:streaming"
            ).apply { acquire(timeoutMs) }
        }
    }

    @Synchronized
    fun release() {
        wakeLock?.let { lock ->
            runCatching { if (lock.isHeld) lock.release() }
        }
        wakeLock = null
    }
}
