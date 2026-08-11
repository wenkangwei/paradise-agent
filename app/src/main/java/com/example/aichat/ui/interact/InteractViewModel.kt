package com.example.aichat.ui.interact

import android.content.Context
import android.media.MediaPlayer
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.example.aichat.data.voice.HttpSttProvider
import com.example.aichat.data.voice.HttpTtsProvider
import com.example.aichat.data.voice.VoiceConfigRepository
import com.example.aichat.di.IoDispatcher
import com.example.aichat.domain.model.Message
import com.example.aichat.domain.model.Role as DomainRole
import com.example.aichat.domain.usecase.SingleShotChatUseCase
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import java.io.File
import javax.inject.Inject
import kotlin.coroutines.resume

/**
 * Drives the Live2D Interact tab's voice conversation loop.
 *
 * State machine:
 *   Idle ──(onRecordStart)──> Recording ──(onRecordStop(wav))──> Transcribing
 *       └─> Thinking ──(LLM stream begins, user msg appended to history)──>
 *       Speaking ──(AI msg appended, deltas streamed, TTS plays)──> Idle
 *
 * Interrupt (`onInterrupt`) cancels the active job at any phase, stops TTS,
 * and drops a trailing empty/partial AI turn.
 *
 * Persistence: NONE for MVP. History lives only in process memory; restart
 * clears it. This matches the "陪伴聊天" semantic — long-term memory is a
 * Stage-E+ concern.
 */
@HiltViewModel
class InteractViewModel @Inject constructor(
    @ApplicationContext private val appContext: Context,
    private val chatUseCase: SingleShotChatUseCase,
    private val voiceConfigRepo: VoiceConfigRepository,
    @IoDispatcher private val io: CoroutineDispatcher,
) : ViewModel() {

    private val _ui = MutableStateFlow(InteractUiState())
    val ui: StateFlow<InteractUiState> = _ui.asStateFlow()

    private var activeJob: Job? = null
    private var ttsMediaPlayer: MediaPlayer? = null
    private var ttsCacheFile: File? = null

    /** Called by the screen when the user presses the push-to-talk button. */
    fun onRecordStart() {
        if (_ui.value.phase.isBusy) return
        _ui.update { it.copy(phase = Phase.Recording, errorMessage = null) }
    }

    /** Called on press release. `wavFile` may be null if recording didn't yield a file. */
    fun onRecordStop(wavFile: File? = null) {
        if (_ui.value.phase != Phase.Recording) return
        if (wavFile == null || !wavFile.exists() || wavFile.length() == 0L) {
            _ui.update { it.copy(phase = Phase.Idle) }
            return
        }
        activeJob = viewModelScope.launch {
            runPipeline(wavFile)
        }
    }

    /** Abort the current pipeline. Idempotent — safe to call from any phase. */
    fun onInterrupt() {
        activeJob?.cancel()
        activeJob = null
        stopTtsPlayback()
        _ui.update { s ->
            val cleaned = s.history.toMutableList().also { list ->
                // Drop trailing AI turn if it's empty or being streamed
                if (list.isNotEmpty() && list.last().role == ChatRole.AI && list.last().text.isBlank()) {
                    list.removeAt(list.lastIndex)
                }
            }
            s.copy(phase = Phase.Idle, history = cleaned)
        }
    }

    private suspend fun runPipeline(wavFile: File) {
        try {
            // 1. STT
            _ui.update { it.copy(phase = Phase.Transcribing) }
            val cfg = voiceConfigRepo.config.value
            val userText = if (cfg.sttUrl.isNotBlank()) {
                val stt = HttpSttProvider(cfg.sttUrl, cfg.sttApiKey.ifBlank { null }, cfg.sttModel)
                withTimeoutOrNull(60_000) { stt.transcribe(wavFile, "zh") }.orEmpty().trim()
            } else {
                ""
            }
            // Best-effort cleanup of the wav file
            runCatching { wavFile.delete() }

            if (userText.isBlank()) {
                _ui.update { it.copy(phase = Phase.Idle, errorMessage = "未识别到语音") }
                return
            }

            // 2. Append user message to history
            val userTurn = ChatTurn(ChatRole.USER, userText, System.currentTimeMillis())
            _ui.update { it.copy(phase = Phase.Thinking, history = it.history + userTurn) }

            // 3. Build LLM messages from history
            val messages = buildLlmMessages(_ui.value.history)

            // 4. Reserve AI turn slot; stream deltas into it
            val aiTurnTimestamp = System.currentTimeMillis()
            _ui.update {
                it.copy(
                    phase = Phase.Speaking,
                    history = it.history + ChatTurn(ChatRole.AI, "", aiTurnTimestamp),
                )
            }

            val aiText = chatUseCase.askStream(messages) { delta ->
                _ui.update { s ->
                    val hist = s.history.toMutableList()
                    if (hist.isNotEmpty() && hist.last().role == ChatRole.AI) {
                        val last = hist.last()
                        hist[hist.lastIndex] = last.copy(text = last.text + delta)
                    }
                    s.copy(history = hist)
                }
            }

            // 5. TTS playback (blocking until completion or cancel)
            if (aiText.isNotBlank()) {
                playTtsBlocking(aiText)
            }

            // 6. Done
            _ui.update { it.copy(phase = Phase.Idle) }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            _ui.update { it.copy(phase = Phase.Idle, errorMessage = "出错了: ${e.message ?: e::class.simpleName}") }
        }
    }

    /**
     * Convert in-memory ChatTurn list into LLM Message list, keeping only the
     * last N turns to bound prompt length. Role mapping: USER→USER, AI→ASSISTANT.
     * Drops turns with empty text (likely a half-streamed AI turn).
     */
    private fun buildLlmMessages(history: List<ChatTurn>): List<Message> {
        val windowed = history.takeLast(MAX_LLM_TURNS)
        return windowed.mapIndexedNotNull { idx, turn ->
            val text = turn.text.trim()
            if (text.isBlank()) return@mapIndexedNotNull null
            Message(
                id = "interact_${turn.timestamp}_$idx",
                conversationId = "interact",
                role = if (turn.role == ChatRole.USER) DomainRole.USER else DomainRole.ASSISTANT,
                content = text,
                timestamp = turn.timestamp,
            )
        }
    }

    /**
     * Synthesize text via the configured TTS endpoint, play it via MediaPlayer,
     * and suspend until playback completes (or is cancelled).
     *
     * Uses HttpTtsProvider which posts OpenAI-compatible {model, input, voice}.
     * Configure VoiceConfig.ttsUrl to the server's /v1/audio/speech endpoint.
     */
    private suspend fun playTtsBlocking(text: String) {
        val cfg = voiceConfigRepo.config.value
        if (cfg.ttsUrl.isBlank()) {
            // No TTS configured — skip playback but don't fail the pipeline.
            return
        }
        val bytes = try {
            val tts = HttpTtsProvider(cfg.ttsUrl, cfg.ttsApiKey.ifBlank { null })
            withContext(io) { tts.synthesize(text, cfg.ttsVoice.ifBlank { "zh-CN-XiaoxiaoNeural" }, cfg.ttsModel) }
        } catch (_: Exception) {
            return
        }
        if (bytes.isEmpty()) return

        withContext(Dispatchers.Main) {
            try {
                val cache = File.createTempFile("tts_interact_", ".mp3", appContext.cacheDir)
                cache.writeBytes(bytes)
                ttsCacheFile = cache

                val mp = MediaPlayer()
                mp.setDataSource(cache.absolutePath)
                mp.setOnCompletionListener { releaseTts() }
                mp.setOnErrorListener { _, _, _ -> releaseTts(); true }
                mp.prepare()
                ttsMediaPlayer = mp
                mp.start()

                // Suspend until the listener fires; cancellation will throw
                // CancellationException out of suspendCancellableCoroutine.
                suspendCancellableCoroutine<Unit> { cont ->
                    val orig = mp.setOnCompletionListener {
                        releaseTts()
                        if (cont.isActive) cont.resume(Unit)
                    }
                    @Suppress("UNUSED_VARIABLE") val _orig = orig
                    cont.invokeOnCancellation { runCatching { mp.release() } }
                }
            } catch (_: Exception) {
                releaseTts()
            }
        }
    }

    private fun stopTtsPlayback() {
        ttsMediaPlayer?.let { mp ->
            runCatching {
                if (mp.isPlaying) mp.stop()
                mp.release()
            }
        }
        ttsMediaPlayer = null
        ttsCacheFile?.let { runCatching { it.delete() } }
        ttsCacheFile = null
    }

    private fun releaseTts() {
        ttsMediaPlayer?.let { mp -> runCatching { mp.release() } }
        ttsMediaPlayer = null
        ttsCacheFile?.let { runCatching { it.delete() } }
        ttsCacheFile = null
    }

    override fun onCleared() {
        super.onCleared()
        activeJob?.cancel()
        releaseTts()
    }

    companion object {
        /** Cap LLM context at the most recent N turns to bound latency + token cost. */
        private const val MAX_LLM_TURNS = 10
    }
}
