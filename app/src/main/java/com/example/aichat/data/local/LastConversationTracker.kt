package com.example.aichat.data.local

import android.content.Context
import dagger.hilt.android.qualifiers.ApplicationContext
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Remembers the conversation the user last had open, so the main process
 * can restore it after being killed by Android (Doze / memory pressure /
 * OEM killer) when the screen locks.
 *
 * Without this:
 *   1. User is mid-chat with conversation C → locks screen.
 *   2. Android kills the main process; the `:streaming` process keeps
 *      running and writes the AI reply to Room.
 *   3. User unlocks → fresh ChatViewModel with `currentConversationId = null`
 *      → UI shows the empty state → user thinks the AI reply disappeared.
 *
 * With this:
 *   On ViewModel init we read the last id, verify the conversation still
 *      exists in Room, and call `selectConversation(id)` to resume.
 *
 * The entry expires after [MAX_AGE_DAYS] so an abandoned conversation from
 * weeks ago doesn't auto-resume.
 */
@Singleton
class LastConversationTracker @Inject constructor(
    @ApplicationContext private val context: Context
) {
    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    fun save(conversationId: String) {
        prefs.edit()
            .putString(KEY_ID, conversationId)
            .putLong(KEY_TS, System.currentTimeMillis())
            .apply()
    }

    /**
     * Returns the saved conversation id, or null if none is recorded or the
     * entry has aged past [MAX_AGE_DAYS].
     */
    fun load(): String? {
        val id = prefs.getString(KEY_ID, null) ?: return null
        val ts = prefs.getLong(KEY_TS, 0L)
        val ageMs = System.currentTimeMillis() - ts
        return if (ageMs in 0..MAX_AGE_MS) id else null
    }

    fun clear() {
        prefs.edit().remove(KEY_ID).remove(KEY_TS).apply()
    }

    private companion object {
        const val PREFS_NAME = "session_state"
        const val KEY_ID = "last_conversation_id"
        const val KEY_TS = "last_conversation_ts"
        val MAX_AGE_DAYS = 7L
        val MAX_AGE_MS = TimeUnit.DAYS.toMillis(MAX_AGE_DAYS)
    }
}
