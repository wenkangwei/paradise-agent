package com.example.aichat.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.net.wifi.WifiManager
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
 * # Multi-session concurrency (v4.0)
 *
 * The service maintains a `Map<conversationId, Job>` instead of a single
 * `streamingJob`. This lets multiple sessions stream concurrently: a user
 * can leave a thinking-model reply running in conversation A while they
 * switch to B and start a new chat. Each job writes to its own row in Room;
 * the UI's per-conversation Flow observer picks up only the relevant slice.
 *
 * The foreground notification is a single instance whose content text is
 * updated as the active count changes ("正在回复 N 个对话...").
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
    /**
     * Active streaming jobs keyed by conversationId. Re-entering the same
     * conversation cancels its prior job; entering a different conversation
     * leaves other jobs running (multi-session concurrency).
     */
    private val streamingJobs = mutableMapOf<String, Job>()
    private val jobsLock = Any()

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // ⚠ Android 14+: startForegroundService() gives us a 5-second window
        // to call startForeground(). The previous implementation returned
        // early on the "already streaming" branch WITHOUT calling it, which
        // reliably crashed with ForegroundServiceDidNotStartInTimeException
        // the moment a user switched sessions and immediately sent a new
        // message. Promote to foreground unconditionally up-front.
        startForeground(NOTIFICATION_ID, buildNotification(activeCount()))

        when (intent?.action ?: ACTION_START) {
            ACTION_START -> {
                val conversationId = intent?.getStringExtra(EXTRA_CONVERSATION_ID)
                val text = intent?.getStringExtra(EXTRA_TEXT) ?: ""
                val attachmentsJson = intent?.getStringExtra(EXTRA_ATTACHMENTS_JSON) ?: "[]"
                val aiMessageId = intent?.getStringExtra(EXTRA_AI_MESSAGE_ID)

                if (conversationId == null || aiMessageId == null) {
                    // Malformed intent — nothing to do. If nothing else is
                    // running, tear down; otherwise stay alive for the others.
                    if (activeCount() == 0) stopSelf()
                    return START_NOT_STICKY
                }

                // Same conversationId re-entered (user re-sent a message in
                // the same session): cancel the prior job so the UseCase's
                // NonCancellable finalize block persists partial content as
                // INTERRUPTED. Other conversations' jobs are untouched.
                synchronized(jobsLock) {
                    streamingJobs.remove(conversationId)?.cancel()
                }

                wakeLockProvider.acquire(this)
                val attachments = parseAttachments(attachmentsJson)
                val job = serviceScope.launch {
                    try {
                        useCase(
                            conversationId = conversationId,
                            text = text,
                            attachments = attachments,
                            aiMessageId = aiMessageId
                        ) { /* errors already persisted via Room by the UseCase */ }
                    } finally {
                        val nowEmpty = synchronized(jobsLock) {
                            streamingJobs.remove(conversationId)
                            streamingJobs.isEmpty()
                        }
                        if (nowEmpty) {
                            wakeLockProvider.release()
                            stopSelf()
                        } else {
                            refreshNotification()
                        }
                    }
                }
                synchronized(jobsLock) {
                    streamingJobs[conversationId] = job
                }
                refreshNotification()
            }
            ACTION_STOP -> {
                // Targeted stop: only the named conversation. If no
                // EXTRA_CONVERSATION_ID is supplied, stop everything.
                val targetConvId = intent?.getStringExtra(EXTRA_CONVERSATION_ID)
                synchronized(jobsLock) {
                    if (targetConvId != null) {
                        streamingJobs.remove(targetConvId)?.cancel()
                    } else {
                        streamingJobs.values.forEach { it.cancel() }
                        streamingJobs.clear()
                    }
                }
                if (activeCount() == 0) {
                    wakeLockProvider.release()
                    stopSelf()
                } else {
                    refreshNotification()
                }
            }
        }
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        synchronized(jobsLock) {
            streamingJobs.values.forEach { it.cancel() }
            streamingJobs.clear()
        }
        serviceScope.cancel()
        runCatching { wakeLockProvider.release() }
        runCatching { stopForeground(STOP_FOREGROUND_DETACH) }
        super.onDestroy()
    }

    private fun activeCount(): Int = synchronized(jobsLock) { streamingJobs.size }

    private fun refreshNotification() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        runCatching {
            nm.notify(NOTIFICATION_ID, buildNotification(activeCount()))
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

    private fun buildNotification(activeCount: Int): Notification {
        val intent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pendingIntent = PendingIntent.getActivity(
            this,
            0,
            intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val title = if (activeCount > 1) "正在回复 $activeCount 个对话..." else "AI 正在回复..."
        val content = if (activeCount > 1)
            "切换应用或锁屏不会中断；可在历史抽屉查看进度"
        else
            "保持前台运行，锁屏或切换应用不会中断"

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(content)
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
        const val EXTRA_AI_MESSAGE_ID = "ai_message_id"

        fun start(
            context: Context,
            conversationId: String,
            text: String,
            attachmentsJson: String,
            aiMessageId: String
        ) {
            val intent = Intent(context, StreamingService::class.java).apply {
                action = ACTION_START
                putExtra(EXTRA_CONVERSATION_ID, conversationId)
                putExtra(EXTRA_TEXT, text)
                putExtra(EXTRA_ATTACHMENTS_JSON, attachmentsJson)
                putExtra(EXTRA_AI_MESSAGE_ID, aiMessageId)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        /**
         * Stop the stream for a specific conversation only. Other in-flight
         * streams are left alone.
         */
        fun stop(context: Context, conversationId: String) {
            val intent = Intent(context, StreamingService::class.java).apply {
                action = ACTION_STOP
                putExtra(EXTRA_CONVERSATION_ID, conversationId)
            }
            runCatching { context.startService(intent) }
        }

        /** Stop every active stream. Use when the user navigates away or the
         *  app is being destroyed. */
        fun stop(context: Context) {
            val intent = Intent(context, StreamingService::class.java).apply {
                action = ACTION_STOP
            }
            runCatching { context.startService(intent) }
        }
    }
}

/**
 * Injectable wrapper around [PowerManager.WakeLock] + [WifiManager.WifiLock].
 *
 * Two locks because WakeLock alone is not enough on aggressive OEM ROMs
 * (Honor MagicOS / Huawei EMUI / Xiaomi MIUI): when the screen turns off,
 * the Wi-Fi chip drops into low-power mode (DTIM scaling, Rx filtering),
 * which causes idle SSE sockets to get RST'd within 1-2 minutes even though
 * the CPU is awake and the process is alive. Acquiring a high-perf WifiLock
 * keeps the radio in active mode so the streaming socket survives screen-off.
 *
 * v4.2.9: WifiLock added specifically to fix "AI 回复在锁屏后中断" on Honor.
 */
class StreamingWakeLock @Inject constructor() {
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

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
        // WIFI_MODE_FULL_HIGH_PERF: 阻止锁屏后 Wi-Fi 进入 PS-Poll / DTIM 省电模式。
        // 没有 this，Honor 锁屏 ~60-120s 内就会主动 RST 长连接（"software caused
        // connection abort"）。注意用 applicationContext 避免 Activity 被销毁时
        // WifiLock 被一并回收。
        runCatching {
            val wm = context.applicationContext
                .getSystemService(Context.WIFI_SERVICE) as WifiManager
            wifiLock = wm.createWifiLock(
                WifiManager.WIFI_MODE_FULL_HIGH_PERF,
                "aichat:streaming-wifi"
            ).apply { acquire() }
        }
    }

    @Synchronized
    fun release() {
        wakeLock?.let { lock ->
            runCatching { if (lock.isHeld) lock.release() }
        }
        wakeLock = null
        wifiLock?.let { lock: WifiManager.WifiLock ->
            runCatching { if (lock.isHeld) lock.release() }
        }
        wifiLock = null
    }
}
