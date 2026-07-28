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
}
