package com.example.aichat.util

import android.app.Application
import android.content.Context
import android.content.Context.MODE_PRIVATE
import com.google.gson.Gson
import java.io.BufferedReader
import java.io.FileReader
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Global uncaught-exception handler that persists the last crash to
 * SharedPreferences, so the next launch can surface it in a UI dialog
 * for the user (and the developer) to inspect.
 *
 * Design constraints:
 *  - **No Hilt / Room dependency.** This runs *before* Hilt is initialised
 *    and must keep working even if the crash is in DB / DI layer.
 *  - **Manual `object` singleton.** Installed from [Application.onCreate]
 *    via [install].
 *  - **Multi-process safe.** Both `main` and `:streaming` processes install
 *    the handler; only the most recent crash is kept (single SP key).
 *  - **Always delegates to the previous handler** after writing, so the OS
 *    still kills the process normally. Otherwise the app would hang.
 *
 * Storage format: a single JSON blob under [KEY_LAST_CRASH] in a private
 * SharedPreferences file. Stacktraces are truncated at 50 KB to avoid
 * pathological OOM chains producing megabytes of output.
 */
object CrashReporter {

    private const val PREFS_NAME = "crash_reporter"
    private const val KEY_LAST_CRASH = "last_crash_json"
    private const val MAX_STACKTRACE_CHARS = 50_000

    private val gson = Gson()
    private val timeFormat =
        SimpleDateFormat("yyyy-MM-dd HH:mm:ss.SSS", Locale.US)

    /**
     * Installs the process-wide [Thread.UncaughtExceptionHandler]. Call
     * this as the **first** line of [Application.onCreate] so it captures
     * every subsequent crash, including ones triggered by Hilt / Room
     * initialisation later in `onCreate` or in `Activity.attachBaseContext`.
     */
    fun install(app: Application) {
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, throwable ->
            // runCatching: never let the reporter itself mask the original
            // crash by throwing during persistence.
            runCatching { writeCrash(app, throwable, thread) }
            // Always delegate so the OS still terminates the process.
            // Without this the process would hang after a crash.
            previous?.uncaughtException(thread, throwable)
        }
    }

    private fun writeCrash(app: Application, throwable: Throwable, thread: Thread) {
        val rawStack = throwable.stackTraceToString()
        val safeStack = if (rawStack.length > MAX_STACKTRACE_CHARS) {
            rawStack.take(MAX_STACKTRACE_CHARS) + "\n...[truncated at $MAX_STACKTRACE_CHARS chars]"
        } else {
            rawStack
        }
        val info = CrashInfo(
            time = System.currentTimeMillis(),
            process = getCurrentProcessName(app),
            thread = thread.name,
            throwableClass = throwable.javaClass.name,
            message = throwable.message,
            stacktrace = safeStack
        )
        val json = gson.toJson(info)
        app.getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
            .edit()
            .putString(KEY_LAST_CRASH, json)
            .apply()
    }

    /**
     * Returns the last crash persisted by [install], or null if none.
     * Read from a [Context] that's typically the Activity or Application.
     */
    fun loadLastCrash(context: Context): CrashInfo? {
        val json = context.getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
            .getString(KEY_LAST_CRASH, null)
            ?: return null
        return runCatching { gson.fromJson(json, CrashInfo::class.java) }.getOrNull()
    }

    /**
     * Removes the persisted crash. Call from the "Clear" button of the
     * crash dialog so the next launch doesn't pop it again.
     */
    fun clearLastCrash(context: Context) {
        context.getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
            .edit()
            .remove(KEY_LAST_CRASH)
            .apply()
    }

    /**
     * Returns the current process name. Used to label whether the crash
     * happened in `main` or `:streaming` — important for diagnosing
     * foreground-service / SSE issues vs UI issues.
     *
     * We read `/proc/self/cmdline` directly rather than calling
     * `Application.getProcessName()` (API 28+): the proc-file approach
     * works on all versions (we target minSdk 26) and avoids any
     * ART-level resolution quirks during crash handling.
     */
    private fun getCurrentProcessName(app: Application): String {
        return readProcessNameFromProc() ?: app.packageName
    }

    private fun readProcessNameFromProc(): String? {
        return try {
            BufferedReader(FileReader("/proc/self/cmdline")).use { reader ->
                reader.readLine()?.trim('\u0000', ' ', '\n')?.takeIf { it.isNotEmpty() }
            }
        } catch (_: Throwable) {
            null
        }
    }

    fun formatTime(epochMillis: Long): String =
        timeFormat.format(Date(epochMillis))
}

/**
 * Persisted crash record. Serialized to JSON by Gson, stored in
 * SharedPreferences under [CrashReporter.KEY_LAST_CRASH].
 *
 * Fields are nullable/lenient where the JVM might not populate them
 * (e.g. `message` is null for some NPEs).
 */
data class CrashInfo(
    val time: Long,
    val process: String,
    val thread: String,
    val throwableClass: String,
    val message: String?,
    val stacktrace: String
) {
    /**
     * Full plain-text rendering suitable for "copy to clipboard" or
     * "share via ACTION_SEND". Includes all metadata + the stacktrace
     * so the recipient has the full picture.
     */
    fun fullText(): String = buildString {
        appendLine("AiChat Crash Report")
        appendLine("Time: ${CrashReporter.formatTime(time)}")
        appendLine("Process: $process")
        appendLine("Thread: $thread")
        appendLine("Exception: $throwableClass")
        if (!message.isNullOrEmpty()) {
            appendLine("Message: $message")
        }
        appendLine()
        appendLine("Stacktrace:")
        append(stacktrace)
    }
}
