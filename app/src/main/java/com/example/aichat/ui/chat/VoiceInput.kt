package com.example.aichat.ui.chat

import android.content.Intent
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.State
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.platform.LocalContext

/**
 * Wraps [SpeechRecognizer] for use in Compose. The recognizer is bound to the
 * composition's lifecycle and destroyed on dispose to avoid leaking the
 * microphone hardware.
 *
 * Press-and-hold usage:
 *   1. Call [VoiceRecognizer.start] on finger-down
 *   2. Call [VoiceRecognizer.stop]  on finger-up
 *   3. [onResult] fires with the transcribed text (or empty string on failure)
 *
 * Notes:
 *   - Requires RECORD_AUDIO runtime permission (caller is responsible).
 *   - Offline-capable on devices that ship a recognition service (most modern
 *     Android phones route this to Google app's on-device ASR).
 *   - Default language is zh-CN since the app targets Chinese users.
 *
 * v4.2.12 #3a (initial): added [rmsLevel] for waveform.
 *
 * v4.2.12 #3a-fix (post-user-test): the original wiring drove [isListening]
 * from `onBeginningOfSpeech`, which on Honor MagicOS frequently never fires
 * (Honor routes SpeechRecognizer to its own voice input service which has
 * inconsistent callback timing). The user pressed the mic, but the waveform
 * placeholder stayed empty. Fix: [isListening] is now set IMMEDIATELY in
 * [start] / [stop] (user-gesture-driven, not callback-driven). A new
 * [isTranscribing] flag covers the "released but waiting for onResults"
 * window so the UI can show a "识别中…" indicator. Together these remove
 * any silent window during a voice session.
 */
data class VoiceRecognizer(
    val start: () -> Unit,
    val stop: () -> Unit,
    val isListening: State<Boolean>,
    val isTranscribing: State<Boolean>,
    val rmsLevel: State<Float>,
    val isAvailable: Boolean
)

@Composable
fun rememberVoiceRecognizer(onResult: (String) -> Unit): VoiceRecognizer {
    val context = LocalContext.current
    val available = remember { SpeechRecognizer.isRecognitionAvailable(context) }
    val listening = remember { mutableStateOf(false) }
    val transcribing = remember { mutableStateOf(false) }
    val rms = remember { mutableStateOf(0f) }

    // Keep the latest callback without re-creating the recognizer on every recomposition
    val resultRef = remember { mutableStateOf(onResult) }
    resultRef.value = onResult

    val recognizer = remember {
        if (available) SpeechRecognizer.createSpeechRecognizer(context) else null
    }

    DisposableEffect(recognizer) {
        recognizer?.setRecognitionListener(object : RecognitionListener {
            override fun onReadyForSpeech(params: Bundle?) {}
            override fun onBeginningOfSpeech() {} // listening now set in start() for instant UI
            override fun onRmsChanged(rmsdB: Float) {
                // rmsdB from Android is dBFS — typically in [-10, 0] for loud
                // speech, droppping to ~-25 dB for background. Map to a 0..1
                // visual range. We use a wide window so even quiet speech
                // produces visible bars; aggressive normalization fits the
                // "waveform feedback" UX better than audiometric accuracy.
                val normalized = ((rmsdB + 18f) / 18f).coerceIn(0.05f, 1f)
                rms.value = normalized
            }
            override fun onBufferReceived(buffer: ByteArray?) {}
            override fun onEndOfSpeech() {} // listening cleared in stop()
            override fun onError(error: Int) {
                listening.value = false
                transcribing.value = false
                rms.value = 0f
                // Errors 6 (no speech) and 7 (no match) → emit empty; others → empty too, UI will just not send
                resultRef.value.invoke("")
            }
            override fun onResults(results: Bundle?) {
                listening.value = false
                transcribing.value = false
                rms.value = 0f
                val list = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                val text = list?.firstOrNull().orEmpty()
                resultRef.value.invoke(text)
            }
            override fun onPartialResults(partialResults: Bundle?) {}
            override fun onEvent(eventType: Int, params: Bundle?) {}
        })
        onDispose { recognizer?.destroy() }
    }

    return VoiceRecognizer(
        start = {
            if (available) {
                // v4.2.12 #3a-fix: set state IMMEDIATELY so the waveform /
                // status bar appears synchronously with the finger press,
                // regardless of whether/when onBeginningOfSpeech fires.
                listening.value = true
                transcribing.value = false
                rms.value = 0f
                val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
                    putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                    putExtra(RecognizerIntent.EXTRA_LANGUAGE, "zh-CN")
                    putExtra(RecognizerIntent.EXTRA_LANGUAGE_PREFERENCE, "zh-CN")
                    putExtra(RecognizerIntent.EXTRA_ONLY_RETURN_LANGUAGE_PREFERENCE, false)
                    putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
                    putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
                }
                runCatching { recognizer?.startListening(intent) }
            }
        },
        stop = {
            // Released by user — flip into "transcribing" until onResults/onError.
            // This window is typically 200–1500ms; the status bar shows 识别中…
            // so the user knows their speech is being processed.
            listening.value = false
            transcribing.value = true
            runCatching { recognizer?.stopListening() }
        },
        isListening = listening,
        isTranscribing = transcribing,
        rmsLevel = rms,
        isAvailable = available
    )
}
