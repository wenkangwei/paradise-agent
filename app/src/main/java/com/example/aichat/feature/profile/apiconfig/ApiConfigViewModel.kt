package com.example.aichat.feature.profile.apiconfig

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.example.aichat.data.provider.BuiltinSuppliers
import com.example.aichat.data.provider.Supplier
import com.example.aichat.data.provider.SupplierRegistry
import com.example.aichat.data.repository.ApiProfile
import com.example.aichat.data.repository.ApiProfileRepository
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import javax.inject.Inject

/**
 * Immutable UI state for the API Config list page.
 */
data class ApiConfigListUiState(
    val profiles: List<ApiProfile> = emptyList(),
    val activeId: String? = null,
    val isLoading: Boolean = true
)

/**
 * Immutable UI state for the edit page (create or edit existing).
 */
data class ApiConfigEditUiState(
    val id: String? = null,
    val title: String = "",
    val supplierId: String = BuiltinSuppliers.fallback.id,
    val baseUrl: String = "",
    val apiKey: String = "",
    val modelName: String = "",
    /**
     * When true, [baseUrl] is the complete chat-completions endpoint and the
     * provider must not append `/chat/completions`. Off by default — every
     * built-in supplier ships a versioned baseUrl that needs the suffix.
     */
    val fullUrlMode: Boolean = false,
    val suppliers: List<Supplier> = emptyList(),
    val apiKeyHistory: List<String> = emptyList(),
    val baseUrlHistory: List<String> = emptyList(),
    val saved: Boolean = false,
    val error: String? = null
) {
    val isNew: Boolean get() = id == null
}

@HiltViewModel
class ApiConfigViewModel @Inject constructor(
    private val repo: ApiProfileRepository,
    private val supplierRegistry: SupplierRegistry
) : ViewModel() {

    val listState: StateFlow<ApiConfigListUiState> = combine(
        repo.observeAll(),
        repo.observeActive()
    ) { profiles, active ->
        ApiConfigListUiState(
            profiles = profiles,
            activeId = active?.id,
            isLoading = false
        )
    }.stateIn(viewModelScope, SharingStarted.Eagerly, ApiConfigListUiState())

    private val _editState = MutableStateFlow(
        ApiConfigEditUiState(suppliers = supplierRegistry.all)
    )
    val editState: StateFlow<ApiConfigEditUiState> = _editState.asStateFlow()

    /**
     * Loads an existing profile into the edit form, or resets to a blank form
     * when [id] is null (create mode). The plaintext API key is intentionally
     * NOT loaded — the user must retype it to change it.
     */
    fun startEdit(id: String?) {
        viewModelScope.launch {
            val suppliers = supplierRegistry.all
            if (id == null) {
                _editState.value = ApiConfigEditUiState(suppliers = suppliers)
                return@launch
            }
            val profile = repo.getById(id)
            if (profile == null) {
                _editState.value = ApiConfigEditUiState(
                    suppliers = suppliers,
                    error = "配置不存在"
                )
                return@launch
            }
            val history = runCatching {
                repo.keyHistoryForSupplier(profile.supplierId)
            }.getOrDefault(emptyList())
            val urlHistory = runCatching { repo.urlHistory(null) }.getOrDefault(emptyList())
            _editState.value = ApiConfigEditUiState(
                id = profile.id,
                title = profile.title,
                supplierId = profile.supplierId,
                baseUrl = profile.baseUrl,
                apiKey = "",
                modelName = profile.modelName,
                fullUrlMode = profile.fullUrlMode,
                suppliers = suppliers,
                apiKeyHistory = history,
                baseUrlHistory = urlHistory
            )
        }
    }

    fun updateTitle(v: String) = _editState.update { it.copy(title = v, saved = false) }
    fun updateSupplier(v: String) {
        // Auto-fill baseUrl/model from the chosen supplier when they're blank
        val supplier = supplierRegistry.byId(v)
        viewModelScope.launch {
            val history = runCatching { repo.keyHistoryForSupplier(v) }.getOrDefault(emptyList())
            val urlHistory = runCatching { repo.urlHistory(v) }.getOrDefault(emptyList())
            _editState.update { s ->
                s.copy(
                    supplierId = v,
                    baseUrl = supplier?.defaultBaseUrl?.ifBlank { s.baseUrl } ?: s.baseUrl,
                    modelName = supplier?.suggestedModels?.firstOrNull()?.ifBlank { s.modelName } ?: s.modelName,
                    apiKeyHistory = history,
                    baseUrlHistory = urlHistory,
                    saved = false
                )
            }
        }
    }

    fun updateBaseUrl(v: String) = _editState.update { it.copy(baseUrl = v, saved = false) }
    fun updateApiKey(v: String) = _editState.update { it.copy(apiKey = v, saved = false) }
    fun updateModel(v: String) = _editState.update { it.copy(modelName = v, saved = false) }
    fun updateFullUrlMode(v: Boolean) =
        _editState.update { it.copy(fullUrlMode = v, saved = false) }
    fun clearSaved() = _editState.update { it.copy(saved = false) }
    fun clearError() = _editState.update { it.copy(error = null) }

    /** Persists the edit form. Returns true on success. */
    fun save(makeDefault: Boolean = false, onDone: (Boolean) -> Unit) {
        val s = _editState.value
        if (s.title.isBlank()) {
            _editState.update { it.copy(error = "标题不能为空") }
            onDone(false); return
        }
        viewModelScope.launch {
            runCatching {
                repo.upsert(
                    id = s.id,
                    title = s.title.trim(),
                    supplierId = s.supplierId,
                    baseUrl = s.baseUrl.trim(),
                    apiKey = s.apiKey,
                    modelName = s.modelName.trim(),
                    makeDefault = makeDefault,
                    fullUrlMode = s.fullUrlMode
                )
            }.onSuccess {
                _editState.update { it.copy(saved = true) }
                onDone(true)
            }.onFailure { err ->
                _editState.update { it.copy(error = err.message ?: "保存失败") }
                onDone(false)
            }
        }
    }

    fun delete(id: String, onDone: () -> Unit) {
        viewModelScope.launch {
            repo.delete(id)
            onDone()
        }
    }

    fun setActive(id: String) {
        viewModelScope.launch { repo.setActive(id) }
    }
}
