package com.example.aichat.data.voice

import java.io.File

/**
 * v4.2.12 #3a: Speech-to-text provider abstraction.
 *
 * Two implementations ship:
 *   - [SystemSpeechRecognizerProvider] — wraps Android SpeechRecognizer (the
 *     legacy on-device ASR used by the chat input bar). No HTTP, no extra
 *     permissions beyond RECORD_AUDIO. Quality varies wildly by OEM ROM.
 *   - [HttpSttProvider] — POSTs a recorded audio file to an OpenAI-compatible
 *     `/v1/audio/transcriptions` endpoint (Whisper, Azure OpenAI Whisper,
 *     self-hosted faster-whisper, etc). Higher quality, costs money, needs
 *     network. Used when the user configures a STT URL in VoiceConfig.
 *
 * Why an interface now (before #3b wires the HTTP path)?
 *   The chat input bar (`ChatInputBar.kt`) has been calling SpeechRecognizer
 *   directly. Splitting the contract out lets #3b's VoiceConfig swap in
 *   HttpSttProvider without touching the input bar again. Keeping the
 *   interface in a separate file also makes mock injection straightforward
 *   in tests.
 */
interface SttProvider {
    /** Whether this provider captures audio itself or expects a recorded file. */
    val capturesAudioInternally: Boolean

    /**
     * Transcribe speech to text.
     *
     * @param audioFile required when [capturesAudioInternally] == false
     *   (i.e. HTTP provider); ignored by SystemSpeechRecognizerProvider.
     * @param language BCP-47 / ISO-639-1 tag like "zh", "en-US". Default "zh".
     * @return transcribed text; empty string on failure / empty audio.
     */
    suspend fun transcribe(audioFile: File?, language: String = "zh"): String
}
