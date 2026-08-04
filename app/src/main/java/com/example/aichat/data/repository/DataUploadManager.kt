package com.example.aichat.data.repository

import android.content.Context
import android.util.Log
import android.os.Build
import android.provider.Settings
import com.example.aichat.data.local.dao.MessageDao
import com.example.aichat.data.local.mapper.toDomain
import com.example.aichat.data.remote.DataCollectionApi
import com.example.aichat.data.remote.dto.ClientInfo
import com.example.aichat.data.remote.dto.ContextInfo

import com.example.aichat.data.remote.dto.UploadRequestDto
import com.example.aichat.di.IoDispatcher
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.util.Locale
import java.util.TimeZone
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Unified data upload manager — handles 3 data types with different upload triggers:
 *
 * 1. session_dialogue — uploaded on session exit (conversation switch / app background)
 * 2. behavior_events — 30s debounce after last interaction (avoid accidental taps)
 * 3. user_profile — uploaded on explicit save
 *
 * WorkManager (FeedbackSyncWorker) calls [syncAllUnsynced] as a periodic fallback.
 */
@Singleton
class DataUploadManager @Inject constructor(
    @ApplicationContext private val context: Context,
    private val messageDao: MessageDao,
    private val api: DataCollectionApi,
    @IoDispatcher private val ioDispatcher: CoroutineDispatcher
) {
    companion object {
        private const val TAG = "DataUploadManager"
        private const val BEHAVIOR_DEBOUNCE_MS = 30_000L
        private const val SESSION_UPLOAD_INTERVAL_MS = 60_000L  // 1 min periodic upload
        private const val PREFS_NAME = "data_upload"
        private const val KEY_SERVER_URL = "server_url"
        private const val KEY_USER_ID = "user_id"
        private const val DEFAULT_SERVER_URL = "http://10.0.2.2:8000" // emulator → host localhost
    }

    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
    private val scope = CoroutineScope(ioDispatcher)

    // Behavior debounce state
    private val pendingBehaviorEvents = ConcurrentHashMap<String, MutableMap<String, Any>>()
    private var behaviorDebounceJob: Job? = null
    private val behaviorLock = Mutex()

    // Session tracking state: conv_id → enter timestamp
    private val sessionEnterTs = ConcurrentHashMap<String, Long>()
    private val lastSyncedTs = ConcurrentHashMap<String, Long>()
    private val sessionUploadJobs = ConcurrentHashMap<String, Job>()
    // conv_id → short session ID (s_<timestamp>)
    private val shortSessionIds = ConcurrentHashMap<String, String>()

    // ── Server URL ───────────────────────────────────────────────

    fun getServerUrl(): String {
        val url = prefs.getString(KEY_SERVER_URL, DEFAULT_SERVER_URL) ?: DEFAULT_SERVER_URL
        return url.trimEnd('/')
    }

    fun updateServerUrl(url: String) {
        prefs.edit().putString(KEY_SERVER_URL, url.trimEnd('/')).apply()
    }

    fun getUserId(): String {
        val existing = prefs.getString(KEY_USER_ID, null)
        if (existing != null) return existing
        val newId = "u_" + UUID.randomUUID().toString().take(12)
        prefs.edit().putString(KEY_USER_ID, newId).apply()
        return newId
    }

    private fun getDeviceId(): String {
        return try {
            Settings.Secure.getString(context.contentResolver, Settings.Secure.ANDROID_ID) ?: "unknown"
        } catch (_: Exception) {
            "unknown"
        }
    }

    private fun getClientInfo(): ClientInfo = ClientInfo(
        userId = getUserId(),
        deviceId = getDeviceId(),
        appVersion = try {
            context.packageManager.getPackageInfo(context.packageName, 0).versionName ?: "unknown"
        } catch (_: Exception) {
            "unknown"
        }
    )

    private fun getContextInfo(): ContextInfo = ContextInfo(
        timestamp = System.currentTimeMillis(),
        locale = Locale.getDefault().toLanguageTag(),
        timezone = TimeZone.getDefault().id
    )

    // ── 1. Session Dialogue ──────────────────────────────────────

    /**
     * Called when user enters a conversation.
     * Starts 1-minute periodic upload timer.
     */
    fun onSessionEnter(convId: String) {
        val now = System.currentTimeMillis()
        sessionEnterTs[convId] = now
        if (!lastSyncedTs.containsKey(convId)) {
            lastSyncedTs[convId] = now
        }
        // Generate short session ID on first enter
        shortSessionIds.putIfAbsent(convId, "s_$now")
        startSessionTimer(convId)
    }

    private fun shortSessionId(convId: String): String {
        return shortSessionIds[convId] ?: "s_${System.currentTimeMillis()}"
    }

    private fun shortSubSessionId(convId: String): String {
        return "s_${sessionEnterTs[convId] ?: System.currentTimeMillis()}"
    }

    /**
     * Called when user leaves (switch, new chat, app close).
     * Immediately uploads any remaining un-uploaded messages, then stops timer.
     */
    suspend fun onSessionLeave(convId: String, exitReason: String = "user_left") {
        Log.d(TAG, "onSessionLeave conv=$convId reason=$exitReason")
        stopSessionTimer(convId)
        uploadSessionMessages(convId, exitReason)
        // Flush behavior events too
        flushBehaviorEvents()
        sessionEnterTs.remove(convId)
    }

    private fun startSessionTimer(convId: String) {
        stopSessionTimer(convId)
        val job = scope.launch {
            while (true) {
                delay(SESSION_UPLOAD_INTERVAL_MS)
                try {
                    uploadSessionMessages(convId, "periodic")
                } catch (_: kotlinx.coroutines.CancellationException) {
                    break
                } catch (_: Exception) {
                    // continue timer on failure
                }
            }
        }
        sessionUploadJobs[convId] = job
    }

    private fun stopSessionTimer(convId: String) {
        sessionUploadJobs.remove(convId)?.cancel()
    }

    /**
     * Upload all un-uploaded messages for a conversation.
     * Creates a new upload batch each time.
     */
    suspend fun uploadSessionMessages(convId: String, trigger: String = "manual") {
        val enterTs = sessionEnterTs[convId] ?: return
        val sinceTs = lastSyncedTs[convId] ?: enterTs
        val now = System.currentTimeMillis()

        withContext(ioDispatcher) {
            val newMessages = messageDao.getMessagesSince(convId, sinceTs)
            if (newMessages.isEmpty()) return@withContext

            val sid = shortSessionId(convId)
            val subId = shortSubSessionId(convId)
            val uploadId = "u_${UUID.randomUUID().toString().take(8)}"
            val messagesPayload = newMessages.map { entity ->
                mapOf(
                    "message_id" to entity.id,
                    "role" to entity.role,
                    "content" to entity.content,
                    "timestamp" to entity.timestamp,
                    "model" to (entity.metadataJson ?: ""),
                    "status" to entity.status
                )
            }

            val payload = mapOf(
                "session_id" to sid,
                "sub_session_id" to subId,
                "upload_id" to uploadId,
                "session_span" to mapOf(
                    "enter_timestamp" to enterTs,
                    "exit_timestamp" to now,
                    "exit_reason" to trigger
                ),
                "incremental_messages" to messagesPayload
            )

            val ok = upload("session_dialogue", payload)
            if (ok) {
                lastSyncedTs[convId] = now
            }
        }
    }

    // ── 2. Behavior Events ───────────────────────────────────────

    /**
     * Queue a behavior event with 30s debounce.
     * Multiple events for the same message within 30s are merged.
     */
    fun queueBehaviorEvent(
        messageId: String,
        sessionId: String,
        eventType: String,
        eventData: Map<String, Any>
    ) {
        Log.d(TAG, "queueBehaviorEvent msg=$messageId type=$eventType")
        val sid = shortSessionId(sessionId)
        val eventId = "evt_${UUID.randomUUID().toString().take(8)}"
        val event = mutableMapOf(
            "event_id" to eventId,
            "message_id" to messageId,
            "session_id" to sid,
            "event_type" to eventType,
            "event_data" to eventData,
            "client_timestamp" to System.currentTimeMillis()
        )
        pendingBehaviorEvents[messageId + eventType] = event

        // Reset debounce timer
        scope.launch {
            behaviorLock.withLock {
                behaviorDebounceJob?.cancel()
                behaviorDebounceJob = scope.launch {
                    delay(BEHAVIOR_DEBOUNCE_MS)
                    // Flush behaviors + trigger session upload for un-uploaded messages
                    flushBehaviorEvents()
                    try { uploadSessionMessages(sessionId, "interaction") } catch (_: Exception) {}
                }
            }
        }
    }

    /**
     * Force-flush pending behavior events immediately (e.g., on session exit).
     */
    suspend fun flushBehaviorEvents() {
        val events = pendingBehaviorEvents.values.toList()
        if (events.isEmpty()) return

        pendingBehaviorEvents.clear()
        withContext(ioDispatcher) {
            val sessionId = events.firstOrNull()?.get("session_id") as? String ?: "unknown"
            val payload = mapOf("session_id" to sessionId, "events" to events)
            upload("behavior_events", payload)
        }
    }

    // ── 3. User Profile ──────────────────────────────────────────

    /**
     * Upload user profile data. Called when user saves profile changes.
     */
    suspend fun uploadProfile(
        profile: Map<String, Any>,
        updatedFields: List<String>? = null
    ) {
        withContext(ioDispatcher) {
            val payload = buildMap<String, Any> {
                put("user_id", getUserId())
                put("profile", profile)
                updatedFields?.let { put("updated_fields", it) }
            }
            upload("user_profile", payload)
        }
    }

    // ── Periodic sync (WorkManager fallback) ─────────────────────

    /**
     * Sync all unsynced feedback data. Called by FeedbackSyncWorker every 30min.
     */
    suspend fun syncAllUnsynced() {
        withContext(ioDispatcher) {
            // Upload unsynced messages as behavior events
            val unsynced = messageDao.getUnsyncedFeedback()
            if (unsynced.isNotEmpty()) {
                val events = unsynced.flatMap { entity ->
                    val sid = shortSessionId(entity.conversationId)
                    val eventsList = mutableListOf<Map<String, Any>>()
                    if (entity.reaction != null) {
                        eventsList.add(mapOf(
                            "event_id" to "evt_${UUID.randomUUID().toString().take(8)}",
                            "message_id" to entity.id,
                            "session_id" to sid,
                            "event_type" to "reaction",
                            "event_data" to mapOf("reaction" to entity.reaction),
                            "client_timestamp" to System.currentTimeMillis()
                        ))
                    }
                    if (entity.interactionsJson != null) {
                        eventsList.add(mapOf(
                            "event_id" to "evt_${UUID.randomUUID().toString().take(8)}",
                            "message_id" to entity.id,
                            "session_id" to sid,
                            "event_type" to "interactions",
                            "event_data" to mapOf("raw" to entity.interactionsJson),
                            "client_timestamp" to System.currentTimeMillis()
                        ))
                    }
                    eventsList
                }

                if (events.isNotEmpty()) {
                    val convId = unsynced.firstOrNull()?.conversationId ?: "unknown"
                    val sid = shortSessionId(convId)
                    val payload = mapOf("session_id" to sid, "events" to events)
                    val success = upload("behavior_events", payload)
                    if (success) {
                        unsynced.forEach { messageDao.updateFeedbackSynced(it.id, 1) }
                    }
                }
            }
        }
    }

    // ── Core upload ──────────────────────────────────────────────

    private suspend fun upload(dataType: String, payload: Map<String, Any>): Boolean {
        val request = UploadRequestDto(
            uploadId = "upload_${UUID.randomUUID()}",
            dataType = dataType,
            payload = payload,
            context = getContextInfo(),
            clientInfo = getClientInfo()
        )

        val baseUrl = getServerUrl()
        val endpoint = when (dataType) {
            "session_dialogue", "behavior_events", "user_profile" -> "$baseUrl/api/data/upload"
            else -> "$baseUrl/api/data/upload"
        }

        return try {
            Log.d(TAG, "upload: type=$dataType to $endpoint")
            val response = api.uploadData(endpoint, request)
            if (response.isSuccessful) {
                Log.d(TAG, "upload: OK type=$dataType")
                true
            } else {
                Log.w(TAG, "upload: FAIL type=$dataType code=${response.code()}")
                false
            }
        } catch (e: Exception) {
            Log.e(TAG, "upload: ERROR type=$dataType msg=${e.message}", e)
            false
        }
    }
}
