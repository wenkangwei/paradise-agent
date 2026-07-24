package com.example.aichat.ui.settings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.example.aichat.data.remote.ConfigManager
import com.example.aichat.domain.model.AppConfig
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import javax.inject.Inject

@HiltViewModel
class SettingsViewModel @Inject constructor(
    private val configManager: ConfigManager
) : ViewModel() {

    private val _uiState = MutableStateFlow(SettingsUiState())
    val uiState: StateFlow<SettingsUiState> = _uiState.asStateFlow()

    init {
        loadSettings()
    }

    private fun loadSettings() {
        val config = configManager.currentConfig()
        _uiState.value = SettingsUiState(
            baseUrl = config.baseUrl,
            apiKey = config.apiKey,
            model = config.model
        )
    }

    fun updateBaseUrl(value: String) {
        _uiState.update { it.copy(baseUrl = value, saved = false) }
    }

    fun updateApiKey(value: String) {
        _uiState.update { it.copy(apiKey = value, saved = false) }
    }

    fun updateModel(value: String) {
        _uiState.update { it.copy(model = value, saved = false) }
    }

    fun saveSettings() {
        viewModelScope.launch {
            val state = _uiState.value
            configManager.updateConfig(
                baseUrl = state.baseUrl,
                apiKey = state.apiKey,
                model = state.model
            )
            _uiState.update { it.copy(saved = true) }
        }
    }

    fun resetSavedState() {
        _uiState.update { it.copy(saved = false) }
    }
}

data class SettingsUiState(
    val baseUrl: String = AppConfig.DEFAULT_BASE_URL,
    val apiKey: String = "",
    val model: String = AppConfig.DEFAULT_MODEL,
    val isTesting: Boolean = false,
    val testResult: TestConnectionResult? = null,
    val saved: Boolean = false
)

enum class TestConnectionResult { SUCCESS, FAILURE }
