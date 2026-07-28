package com.example.aichat.feature.voice

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.example.aichat.data.voice.VoiceConfig
import com.example.aichat.data.voice.VoiceConfigRepository
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

/**
 * v4.2.12 #3b: ViewModel backing [VoiceConfigPage]. Owns a local mutable
 * form state so the user can edit freely; the persisted [VoiceConfigRepository]
 * is only written when they hit "保存".
 *
 * Why a separate form state (and not just write-through to the repository)?
 *   - Cancel-vs-save semantics: the user can tweak fields, hit back, and
 *     nothing is persisted. Matches the UX of ApiConfigEditPage.
 *   - Avoids encrypting on every keystroke.
 */
@HiltViewModel
class VoiceConfigViewModel @Inject constructor(
    private val repository: VoiceConfigRepository
) : ViewModel() {

    private val _form = MutableStateFlow(repository.config.value)
    val form: StateFlow<VoiceConfig> = _form.asStateFlow()

    val saved: StateFlow<VoiceConfig> = repository.config

    fun updateSttUrl(v: String) { _form.value = _form.value.copy(sttUrl = v) }
    fun updateSttKey(v: String) { _form.value = _form.value.copy(sttApiKey = v) }
    fun updateSttModel(v: String) { _form.value = _form.value.copy(sttModel = v) }
    fun updateTtsUrl(v: String) { _form.value = _form.value.copy(ttsUrl = v) }
    fun updateTtsKey(v: String) { _form.value = _form.value.copy(ttsApiKey = v) }
    fun updateTtsModel(v: String) { _form.value = _form.value.copy(ttsModel = v) }
    fun updateTtsVoice(v: String) { _form.value = _form.value.copy(ttsVoice = v) }

    fun save(onDone: () -> Unit) {
        viewModelScope.launch {
            repository.save(_form.value)
            onDone()
        }
    }
}
