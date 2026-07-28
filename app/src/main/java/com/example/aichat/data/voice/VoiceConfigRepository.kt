package com.example.aichat.data.voice

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import com.example.aichat.di.IoDispatcher
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.withContext
import javax.inject.Inject
import javax.inject.Singleton

/**
 * v4.2.12 #3b: Stores [VoiceConfig] in an EncryptedSharedPreferences file.
 *
 * Why encrypted (and not plain SharedPreferences / Room)?
 *   - STT/TTS API keys are bearer credentials. On a rooted device or via
 *     `adb backup`, plain SharedPrefs are world-readable text. The Android
 *     Keystore-backed AES-256-GCM used by EncryptedSharedPreferences is the
 *     same envelope [ApiKeyEncryptor] uses for chat-completion API keys, so
 *     we get hardware-backed protection with zero new infrastructure.
 *
 * Why StateFlow (and not just suspend get/set)?
 *   - The settings page writes; the chat input bar + message bubble read.
 *   StateFlow lets readers observe reactively without re-loading on every
 *   screen transition. Initial value is loaded eagerly in the constructor
 *   on the IO dispatcher so first read is non-blocking after the warm-up.
 */
@Singleton
class VoiceConfigRepository @Inject constructor(
    @ApplicationContext private val context: Context,
    @IoDispatcher private val ioDispatcher: CoroutineDispatcher
) {
    private val prefs: SharedPreferences = run {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        EncryptedSharedPreferences.create(
            context,
            FILE_NAME,
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
        )
    }

    private val _config = MutableStateFlow(load())
    val config: StateFlow<VoiceConfig> = _config.asStateFlow()

    private fun load(): VoiceConfig = VoiceConfig(
        sttUrl = prefs.getString(KEY_STT_URL, "") ?: "",
        sttApiKey = prefs.getString(KEY_STT_KEY, "") ?: "",
        sttModel = prefs.getString(KEY_STT_MODEL, "whisper-1") ?: "whisper-1",
        ttsUrl = prefs.getString(KEY_TTS_URL, "") ?: "",
        ttsApiKey = prefs.getString(KEY_TTS_KEY, "") ?: "",
        ttsModel = prefs.getString(KEY_TTS_MODEL, "tts-1") ?: "tts-1",
        ttsVoice = prefs.getString(KEY_TTS_VOICE, "alloy") ?: "alloy"
    )

    /** Persist [new] and emit on [config]. */
    suspend fun save(new: VoiceConfig) = withContext(ioDispatcher) {
        prefs.edit()
            .putString(KEY_STT_URL, new.sttUrl.trim())
            .putString(KEY_STT_KEY, new.sttApiKey.trim())
            .putString(KEY_STT_MODEL, new.sttModel.trim().ifEmpty { "whisper-1" })
            .putString(KEY_TTS_URL, new.ttsUrl.trim())
            .putString(KEY_TTS_KEY, new.ttsApiKey.trim())
            .putString(KEY_TTS_MODEL, new.ttsModel.trim().ifEmpty { "tts-1" })
            .putString(KEY_TTS_VOICE, new.ttsVoice.trim().ifEmpty { "alloy" })
            .apply()
        _config.value = new
    }

    companion object {
        private const val FILE_NAME = "aichat_voice_config"
        private const val KEY_STT_URL = "stt_url"
        private const val KEY_STT_KEY = "stt_key"
        private const val KEY_STT_MODEL = "stt_model"
        private const val KEY_TTS_URL = "tts_url"
        private const val KEY_TTS_KEY = "tts_key"
        private const val KEY_TTS_MODEL = "tts_model"
        private const val KEY_TTS_VOICE = "tts_voice"
    }
}
