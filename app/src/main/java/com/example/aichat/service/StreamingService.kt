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
import dagger.hilt.android.AndroidEntryPoint
import javax.inject.Inject

/**
 * Foreground service that keeps the app process alive while an AI stream is
 * running, so that locking the screen or switching apps does not abort the
 * SSE connection.
 *
 * The service does not perform the request itself - [com.example.aichat.ui.chat.ChatViewModel]
 * runs the streaming coroutine in an application-level scope. This service
 * merely elevates the process to foreground priority and holds a WakeLock so
 * Android Doze / app standby cannot freeze the network stack.
 */
@AndroidEntryPoint
class StreamingService : Service() {

    @Inject
    lateinit var wakeLockProvider: StreamingWakeLock

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.action ?: ACTION_START
        when (action) {
            ACTION_START -> {
                startForeground(NOTIFICATION_ID, buildNotification())
                wakeLockProvider.acquire(this)
            }
            ACTION_STOP -> {
                wakeLockProvider.release()
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
            }
        }
        return START_NOT_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        wakeLockProvider.release()
        super.onDestroy()
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "AI 流式回复",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "保持后台连接，让 AI 回复在锁屏时也能继续"
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

        fun start(context: Context) {
            val intent = Intent(context, StreamingService::class.java).apply {
                action = ACTION_START
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
 * Injectable wrapper around [PowerManager.WakeLock] so both the Service and the
 * ViewModel can request CPU wake without duplicating the logic.
 */
class StreamingWakeLock {
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
