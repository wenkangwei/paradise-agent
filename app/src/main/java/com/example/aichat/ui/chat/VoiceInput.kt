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
 */
data class VoiceRecognizer(
    val start: () -> Unit,
    val stop: () -> Unit,
    val isListening: State<Boolean>,
    val isAvailable: Boolean
)

@Composable
fun rememberVoiceRecognizer(onResult: (String) -> Unit): VoiceRecognizer {
    val context = LocalContext.current
    val available = remember { SpeechRecognizer.isRecognitionAvailable(context) }
    val listening = remember { mutableStateOf(false) }

    // Keep the latest callback without re-creating the recognizer on every recomposition
    val resultRef = remember { mutableStateOf(onResult) }
    resultRef.value = onResult

    val recognizer = remember {
        if (available) SpeechRecognizer.createSpeechRecognizer(context) else null
    }

    DisposableEffect(recognizer) {
        recognizer?.setRecognitionListener(object : RecognitionListener {
            override fun onReadyForSpeech(params: Bundle?) {}
            override fun onBeginningOfSpeech() { listening.value = true }
            override fun onRmsChanged(rmsdB: Float) {}
            override fun onBufferReceived(buffer: ByteArray?) {}
            override fun onEndOfSpeech() { listening.value = false }
            override fun onError(error: Int) {
                listening.value = false
                // Errors 6 (no speech) and 7 (no match) → emit empty; others → empty too, UI will just not send
                resultRef.value.invoke("")
            }
            override fun onResults(results: Bundle?) {
                listening.value = false
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
            runCatching { recognizer?.stopListening() }
        },
        isListening = listening,
        isAvailable = available
    )
}
