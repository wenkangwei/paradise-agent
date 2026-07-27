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
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
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

    // v4.2.10: Honor MagicOS 用通知文本变化判定 FGS "活跃度"。静态文本触发
    // 锁屏 60-120s 后回收。ticker 每 15s 刷新一次通知，让 Honor 判定活跃。
    private var tickerJob: Job? = null
    private var firstJobStartedAt: Long? = null

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
                // v4.2.10: 启动 ticker 让通知文本随时间变化，对抗 Honor MagicOS
                // 的 FGS 活跃度判定。首个 job 启动时记下时间戳。
                synchronized(jobsLock) {
                    if (tickerJob?.isActive != true) {
                        firstJobStartedAt = System.currentTimeMillis()
                        tickerJob = serviceScope.launch {
                            while (isActive) {
                                delay(TICKER_INTERVAL_MS)
                                refreshNotification()
                            }
                        }
                    }
                }
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
                            // v4.2.10: 最后一个 job 结束，停 ticker + 清状态
                            tickerJob?.cancel()
                            tickerJob = null
                            firstJobStartedAt = null
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
            tickerJob?.cancel()
            tickerJob = null
            firstJobStartedAt = null
        }
        serviceScope.cancel()
        runCatching { wakeLockProvider.release() }
        runCatching { stopForeground(STOP_FOREGROUND_DETACH) }
        super.onDestroy()
    }

    private fun activeCount(): Int = synchronized(jobsLock) { streamingJobs.size }

    private fun refreshNotification(elapsedSec: Int = currentElapsedSec()) {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        runCatching {
            nm.notify(NOTIFICATION_ID, buildNotification(activeCount(), elapsedSec))
        }
    }

    /**
     * 自首个 active job 启动以来经过的秒数。用于让通知文本随时间变化，
     * Honor MagicOS 据此判定 FGS "活跃"而非"死任务"。
     */
    private fun currentElapsedSec(): Int {
        val start = firstJobStartedAt ?: return 0
        return ((System.currentTimeMillis() - start) / 1000).toInt()
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            // v4.2.10: 旧 channel 是 IMPORTANCE_LOW，Honor MagicOS 据此判定
            // FGS 为"低优先级"→ 锁屏 60-120s 内主动 RST socket / 杀进程。
            // Android 不允许升级已存在 channel 的 importance，必须删旧建新。
            runCatching { nm.deleteNotificationChannel(LEGACY_CHANNEL_ID) }
            val channel = NotificationChannel(
                CHANNEL_ID,
                "AI 流式回复",
                NotificationManager.IMPORTANCE_DEFAULT
            ).apply {
                description = "保持后台连接，让 AI 回复在锁屏或切换应用时继续"
                setShowBadge(false)
                // IMPORTANCE_DEFAULT 默认带声音+震动，强制无声无震避免打扰用户。
                // 关键点：Honor 看的是 importance 级别，不是声音。级别到位就行。
                setSound(null, null)
                enableVibration(false)
                lockscreenVisibility = Notification.VISIBILITY_PUBLIC
            }
            nm.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(activeCount: Int, elapsedSec: Int = 0): Notification {
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
        // v4.2.10: 流过程中文本随 elapsedSec 变化，让 Honor 判定 FGS 活跃。
        // 静态文本（v4.2.9 之前）会被视为"死任务"主动回收。
        val content = when {
            activeCount > 1 -> "切换应用或锁屏不会中断；$elapsedSec 秒"
            elapsedSec > 0 -> "已持续 $elapsedSec 秒，锁屏不会中断"
            else -> "保持前台运行，锁屏或切换应用不会中断"
        }

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(content)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            // v4.2.10: PRIORITY_DEFAULT + VISIBILITY_PUBLIC 让 Honor MagicOS
            // 把 FGS 视为"用户关心"的活跃任务。之前 LOW + setSilent 触发
            // Honor 的"可回收"判定，锁屏后被杀。
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .build()
    }

    companion object {
        // v4.2.10: 换 channel id 让 IMPORTANCE 升级生效（Android 不允许升级
        // 已存在 channel 的 importance）。旧 channel 在 createNotificationChannel
        // 里被显式删除，避免残留两条。
        private const val LEGACY_CHANNEL_ID = "streaming_channel"
        private const val CHANNEL_ID = "streaming_channel_v2"
        private const val NOTIFICATION_ID = 0x5354_5245 // "STRE"
        // v4.2.10: 每 15s 刷一次通知文本，让 Honor 判定 FGS 活跃。
        // 太频繁（<10s）可能被系统判为 spam 限频，太稀疏（>30s）则可能在
        // Honor 的 60s 检查窗口内只刷一次（不够）。15s 是经验值。
        private const val TICKER_INTERVAL_MS = 15_000L
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
