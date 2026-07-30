package com.example.aichat.data.voice

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * v4.2.12 #3b: Text-to-speech provider abstraction.
 *
 * [HttpTtsProvider] implements the OpenAI `/v1/audio/speech` contract — the
 * de-facto standard now supported by Azure OpenAI TTS, self-hosted
 * XTTS / piper / openedai-tts, and most cloud gateways. The returned bytes
 * are mp3 by default (model=tts-1 doesn't natively emit wav).
 *
 * No system TTS fallback: Android's TextToSpeech engine quality varies by
 * OEM and tends to be dramatically worse than Whisper-1 / XTTS for Chinese
 * text. Users who want TTS at all will want the HTTP path. If we later add
 * a system path, it can implement the same interface.
 */
interface TtsProvider {
    /**
     * Synthesize [text] to audio bytes (default mp3).
     * @return raw audio bytes; empty ByteArray on failure.
     */
    suspend fun synthesize(text: String, voice: String = "alloy", model: String = "tts-1"): ByteArray
}

class AndroidTtsProvider(
    private val context: android.content.Context
) : TtsProvider {
    override suspend fun synthesize(text: String, voice: String, model: String): ByteArray {
        // Android built-in TTS writes to a file, then read back as bytes
        val file = java.io.File(context.cacheDir, "tts_android_${System.currentTimeMillis()}.wav")
        val lock = java.util.concurrent.CountDownLatch(1)
        var success = false
        val tts = android.speech.tts.TextToSpeech(context) { status ->
            // init callback — status == TextToSpeech.SUCCESS means engine ready
        }
        tts.language = java.util.Locale.CHINESE
        tts.setSpeechRate(1.0f)
        tts.setOnUtteranceProgressListener(object : android.speech.tts.UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) {}
            override fun onDone(utteranceId: String?) { lock.countDown() }
            override fun onError(utteranceId: String?) { lock.countDown() }
            @Deprecated("Deprecated in Java")
            override fun onError(utteranceId: String?, errorCode: Int) { lock.countDown() }
        })
        val result = tts.synthesizeToFile(text, null, file, "tts_1")
        if (result == android.speech.tts.TextToSpeech.SUCCESS) {
            lock.await(30, java.util.concurrent.TimeUnit.SECONDS)
            if (file.exists() && file.length() > 0) {
                success = true
            }
        }
        tts.shutdown()
        return if (success) file.readBytes() else ByteArray(0)
    }
}

class HttpTtsProvider(
    private val ttsUrl: String,
    private val ttsApiKey: String?,
    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()
) : TtsProvider {

    override suspend fun synthesize(text: String, voice: String, model: String): ByteArray {
        if (text.isBlank()) return ByteArray(0)
        val payload = JSONObject()
            .put("model", model)
            .put("input", text)
            .put("voice", voice)
            .put("response_format", "mp3")
            .toString()

        val request = Request.Builder()
            .url(ttsUrl)
            .apply { if (!ttsApiKey.isNullOrEmpty()) addHeader("Authorization", "Bearer $ttsApiKey") }
            .post(payload.toRequestBody("application/json".toMediaType()))
            .build()

        return runCatching {
            client.newCall(request).execute().use { resp ->
                if (!resp.isSuccessful) ByteArray(0)
                else resp.body?.bytes() ?: ByteArray(0)
            }
        }.getOrElse { ByteArray(0) }
    }
}
