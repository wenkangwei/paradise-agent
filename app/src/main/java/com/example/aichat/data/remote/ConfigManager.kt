package com.example.aichat.data.remote

import android.content.Context
import android.content.SharedPreferences
import com.example.aichat.domain.model.AppConfig
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Manages runtime [AppConfig] backed by SharedPreferences.
 *
 * Provides read/write access to baseUrl, apiKey, and model.
 * The in-memory [AppConfig] singleton used for DI is updated when settings change.
 */
@Singleton
class ConfigManager @Inject constructor(
    context: Context
) {
    private val prefs: SharedPreferences =
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    @Volatile
    private var _config: AppConfig = loadConfig()

    /**
     * The current config value, always reflecting the latest [updateConfig] call.
     */
    fun currentConfig(): AppConfig = _config

    /**
     * Updates the config both in memory and in SharedPreferences.
     */
    fun updateConfig(baseUrl: String?, apiKey: String?, model: String?) {
        val newConfig = _config.copy(
            baseUrl = baseUrl?.takeIf { it.isNotBlank() } ?: _config.baseUrl,
            apiKey = apiKey ?: _config.apiKey,
            model = model?.takeIf { it.isNotBlank() } ?: _config.model
        )
        prefs.edit().apply {
            putString(KEY_BASE_URL, newConfig.baseUrl)
            putString(KEY_API_KEY, newConfig.apiKey)
            putString(KEY_MODEL, newConfig.model)
            apply()
        }
        _config = newConfig
    }

    private fun loadConfig(): AppConfig {
        return AppConfig(
            baseUrl = prefs.getString(KEY_BASE_URL, AppConfig.DEFAULT_BASE_URL)
                ?: AppConfig.DEFAULT_BASE_URL,
            apiKey = prefs.getString(KEY_API_KEY, "") ?: "",
            model = prefs.getString(KEY_MODEL, AppConfig.DEFAULT_MODEL)
                ?: AppConfig.DEFAULT_MODEL
        )
    }

    companion object {
        private const val PREFS_NAME = "ai_chat_config"
        private const val KEY_BASE_URL = "base_url"
        private const val KEY_API_KEY = "api_key"
        private const val KEY_MODEL = "model"
    }
}
