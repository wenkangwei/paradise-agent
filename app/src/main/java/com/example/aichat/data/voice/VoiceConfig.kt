package com.example.aichat.data.voice

/**
 * v4.2.12 #3b: User configuration for STT (speech-to-text) and TTS
 * (text-to-speech) services.
 *
 * Fields:
 *   - [sttUrl] / [sttApiKey] — when set, voice input records audio to a file
 *     and POSTs it to this OpenAI-Whisper-compatible endpoint
 *     (`/v1/audio/transcriptions`). Empty → fall back to Android system
 *     SpeechRecognizer (the legacy on-device ASR).
 *   - [ttsUrl] / [ttsApiKey] / [ttsModel] / [ttsVoice] — when set, the
 *     speaker icon on AI message bubbles synthesises speech via this
 *     OpenAI-TTS-compatible endpoint (`/v1/audio/speech`). Empty → the
 *     speaker icon is hidden (no TTS).
 *
 * All fields default to empty so a fresh install has no voice services
 * configured (matches the current behaviour pre-#3b).
 */
data class VoiceConfig(
    val sttUrl: String = "",
    val sttApiKey: String = "",
    val sttModel: String = "whisper-1",
    val ttsUrl: String = "",
    val ttsApiKey: String = "",
    val ttsModel: String = "tts-1",
    val ttsVoice: String = "alloy"
) {
    val hasStt: Boolean get() = sttUrl.isNotBlank()
    val hasTts: Boolean get() = ttsUrl.isNotBlank()

    /**
     * Effective TTS endpoint: explicitly configured ttsUrl, or derived from
     * sttUrl's server (same host:port, path /v1/audio/speech). This lets TTS
     * "just work" when the user has configured STT but hasn't bothered to set
     * a separate TTS URL — the companion server exposes both endpoints.
     */
    val resolvedTtsUrl: String
        get() {
            if (ttsUrl.isNotBlank()) return ttsUrl
            if (sttUrl.isBlank()) return ""
            val schemeEnd = sttUrl.indexOf("://")
            if (schemeEnd < 0) return ""
            val scheme = sttUrl.substring(0, schemeEnd)
            val afterScheme = sttUrl.substring(schemeEnd + 3)
            val pathStart = afterScheme.indexOf("/")
            val hostPort = if (pathStart > 0) afterScheme.substring(0, pathStart) else afterScheme
            return "$scheme://$hostPort/v1/audio/speech"
        }

    /**
     * Voice to pass to edge-tts. The config default "alloy" is an OpenAI voice
     * name that edge-tts doesn't recognise — fall back to a Chinese female voice.
     */
    val resolvedTtsVoice: String
        get() = when {
            ttsVoice.isBlank() -> "zh-CN-XiaoxiaoNeural"
            ttsVoice == "alloy" -> "zh-CN-XiaoxiaoNeural"
            else -> ttsVoice
        }
}
