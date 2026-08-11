package com.example.aichat.data.voice

import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * v4.2.12 #3a: HTTP-based STT provider — POSTs a recorded audio file to an
 * OpenAI-compatible `/v1/audio/transcriptions` endpoint.
 *
 * Contract (per OpenAI spec, also followed by Azure OpenAI Whisper,
 * faster-whisper-server, whisper-cpp's server example, etc):
 *
 *   POST {sttUrl}
 *   Authorization: Bearer {sttApiKey}
 *   Content-Type: multipart/form-data
 *
 *   multipart fields:
 *     file:        <audio bytes>          (required)
 *     model:       "whisper-1" / etc      (required by OpenAI; some self-hosted accept "")
 *     language:    "zh" / "en"            (optional, improves accuracy)
 *     response_format: "json" / "text"    (optional, defaults to json)
 *
 * Response (json):
 *   { "text": "transcribed text here" }
 *
 * Wiring to the UI is performed in #3b via VoiceConfig / VoiceConfigRepository
 * once the user has a place to enter the URL + key.
 *
 * NOTE: this class is intentionally framework-light — no Retrofit, no Hilt
 * qualifier. The chat app already uses OkHttp elsewhere; reusing it keeps
 * the dependency surface flat. A 30-second connect/read timeout matches
 * Whisper's typical 1-5s latency with headroom for cold-start on
 * self-hosted deployments.
 */
class HttpSttProvider(
    private val sttUrl: String,
    private val sttApiKey: String?,
    /** Optional: model name (e.g. "whisper-1"). Empty string → omit from form. */
    private val model: String = "whisper-1",
    /** Allows tests to inject a mock client; production uses default timeouts. */
    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()
) : SttProvider {

    override val capturesAudioInternally: Boolean = false

    override suspend fun transcribe(audioFile: File?, language: String): String {
        val file = audioFile ?: return ""
        if (!file.exists() || file.length() == 0L) return ""

        val mime = guessAudioMime(file.name)
        val filePart = MultipartBody.Part.createFormData(
            name = "file",
            filename = file.name,
            body = file.asRequestBody(mime.toMediaTypeOrNull())
        )

        val builder = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addPart(filePart)

        if (model.isNotEmpty()) {
            builder.addFormDataPart("model", model)
        }
        if (language.isNotEmpty()) {
            builder.addFormDataPart("language", language)
        }
        builder.addFormDataPart("response_format", "json")

        val request = Request.Builder()
            .url(sttUrl)
            .apply { if (!sttApiKey.isNullOrEmpty()) addHeader("Authorization", "Bearer $sttApiKey") }
            .post(builder.build())
            .build()

        return try {
            client.newCall(request).execute().use { resp ->
                android.util.Log.d("HttpSttProvider",
                    "resp code=${resp.code} msg=${resp.message} url=$sttUrl")
                if (!resp.isSuccessful) {
                    val errBody = runCatching { resp.body?.string().orEmpty() }.getOrDefault("")
                    android.util.Log.d("HttpSttProvider", "error body: ${errBody.take(300)}")
                    return@use ""
                }
                val body = resp.body?.string().orEmpty()
                android.util.Log.d("HttpSttProvider", "body len=${body.length} preview=${body.take(200)}")
                // OpenAI Whisper always returns JSON with a "text" field when
                // response_format=json. Some self-hosted variants ignore the
                // requested format and return plain text — handle both.
                runCatching {
                    JSONObject(body).optString("text", "")
                }.getOrElse { body.trim() }
            }
        } catch (e: kotlinx.coroutines.CancellationException) {
            throw e
        } catch (e: Exception) {
            android.util.Log.e("HttpSttProvider", "transcribe FAILED", e)
            ""
        }
    }

    /** Map a few common audio extensions to MIME types OkHttp will accept. */
    private fun guessAudioMime(name: String): String {
        val ext = name.substringAfterLast('.', missingDelimiterValue = "").lowercase()
        return when (ext) {
            "mp3" -> "audio/mpeg"
            "m4a" -> "audio/mp4"
            "wav" -> "audio/wav"
            "webm" -> "audio/webm"
            "ogg" -> "audio/ogg"
            "flac" -> "audio/flac"
            "aac" -> "audio/aac"
            else -> "application/octet-stream"
        }
    }
}
