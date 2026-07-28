package com.example.aichat.data.voice

import android.content.Context
import android.media.MediaPlayer
import com.example.aichat.di.IoDispatcher
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob
import okhttp3.OkHttpClient
import java.io.File
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Singleton

/**
 * v4.2.12 #3b: Singleton TTS playback controller.
 *
 * Owns one [MediaPlayer] at a time and exposes which message id (if any) is
 * currently being spoken. The chat UI reacts to [speakingMessageId] to swap
 * the play/stop icon on the relevant bubble.
 *
 * Lifecycle:
 *   - The controller survives configuration changes because it's @Singleton.
 *   - The MediaPlayer is released on [stop] (also when synthesis fails so we
 *     don't leave a stale speakingMessageId pointing at finished audio).
 *   - The cache file is deleted after playback completes / errors out; this
 *     avoids unbounded growth under heavy use.
 *
 * Why a controller (and not just inline MediaPlayer in the composable)?
 *   - Compose recomposition would recreate MediaPlayer repeatedly.
 *   - One controller gives us single-source-of-truth for "what's playing"
 *     so starting a new playback auto-stops the previous one.
 */
@Singleton
class TtsController @Inject constructor(
    @ApplicationContext private val appContext: Context,
    @IoDispatcher private val ioDispatcher: CoroutineDispatcher
) {
    private val _speakingMessageId = MutableStateFlow<String?>(null)
    val speakingMessageId: StateFlow<String?> = _speakingMessageId.asStateFlow()

    private var mediaPlayer: MediaPlayer? = null
    private var cacheFile: File? = null
    private val scope = CoroutineScope(SupervisorJob() + ioDispatcher)
    private val client = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    /**
     * Synthesize [text] via [provider] and start playback for [messageId].
     * Stops any current playback first. No-op if [text] is blank.
     */
    fun play(messageId: String, text: String, provider: TtsProvider) {
        if (text.isBlank()) return
        stop()
        _speakingMessageId.value = messageId
        scope.launch {
            val bytes = withContext(ioDispatcher) { provider.synthesize(text) }
            if (bytes.isEmpty()) {
                _speakingMessageId.value = null
                return@launch
            }
            val cache = File(appContext.cacheDir, "tts_${System.currentTimeMillis()}.mp3").apply {
                writeBytes(bytes)
            }
            cacheFile = cache
            try {
                MediaPlayer().apply {
                    setDataSource(cache.absolutePath)
                    setOnCompletionListener {
                        _speakingMessageId.value = null
                        releasePlayer()
                    }
                    setOnErrorListener { _, _, _ ->
                        _speakingMessageId.value = null
                        releasePlayer()
                        true
                    }
                    prepare()
                    start()
                }.also { mediaPlayer = it }
            } catch (e: Exception) {
                _speakingMessageId.value = null
                releasePlayer()
            }
        }
    }

    /** Stop playback (if any) and clear state. Safe to call repeatedly. */
    fun stop() {
        releasePlayer()
        _speakingMessageId.value = null
    }

    private fun releasePlayer() {
        mediaPlayer?.let { runCatching { it.stop() } }
        mediaPlayer?.release()
        mediaPlayer = null
        cacheFile?.let { runCatching { it.delete() } }
        cacheFile = null
    }
}
