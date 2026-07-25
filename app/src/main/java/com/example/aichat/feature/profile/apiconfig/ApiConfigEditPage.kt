package com.example.aichat.feature.profile.apiconfig

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.History
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExposedDropdownMenuBox
import androidx.compose.material3.ExposedDropdownMenuDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.example.aichat.data.provider.Supplier

/**
 * Create/edit form for a single API profile.
 *
 * Fields: 标题 / 供应商 / Base URL / API Key / Model Name
 *
 * - Supplier dropdown auto-fills baseUrl/model with the supplier's defaults.
 * - Base URL & API Key expose a "history" dropdown (📋 button) listing values
 *   the user has entered in other profiles, so common configs are one-tap.
 * - Model field shows the supplier's suggested models as a dropdown.
 * - API Key uses PasswordVisualTransformation; for edits the key field is
 *   always blank (caller must retype or pick from history to change).
 *
 * [onSaved] is invoked after a successful save; [onBack] for the up button.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ApiConfigEditPage(
    profileId: String?,
    onBack: () -> Unit,
    onSaved: () -> Unit,
    viewModel: ApiConfigViewModel = hiltViewModel()
) {
    val state by viewModel.editState.collectAsStateWithLifecycle()

    LaunchedEffect(profileId) {
        viewModel.startEdit(profileId)
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(if (state.isNew) "新建 API 配置" else "编辑 API 配置") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "返回")
                    }
                }
            )
        }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            OutlinedTextField(
                value = state.title,
                onValueChange = viewModel::updateTitle,
                label = { Text("标题") },
                placeholder = { Text("如：我的 DeepSeek") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth()
            )

            SupplierSelector(
                selectedId = state.supplierId,
                suppliers = state.suppliers,
                onSelect = viewModel::updateSupplier
            )

            // Base URL with history dropdown
            HistoryTextField(
                value = state.baseUrl,
                onValueChange = viewModel::updateBaseUrl,
                label = "Base URL",
                placeholder = if (state.fullUrlMode) {
                    "https://api.example.com/v1/chat/completions"
                } else {
                    "https://api.example.com/v1/"
                },
                history = state.baseUrlHistory,
                keyboardType = KeyboardType.Uri,
                mask = false
            )

            // Toggle: is baseUrl the complete endpoint?
            // On  → baseUrl is used verbatim, no suffix appended (exotic gateways,
            //      custom OpenAI-compatible routers with non-standard paths).
            // Off → the provider appends `chat/completions` (default for every
            //      built-in supplier).
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.fillMaxWidth()
            ) {
                Switch(
                    checked = state.fullUrlMode,
                    onCheckedChange = viewModel::updateFullUrlMode
                )
                Spacer(Modifier.width(12.dp))
                Column {
                    Text(
                        text = "完整 URL（不拼接后缀）",
                        style = MaterialTheme.typography.bodyMedium
                    )
                    Text(
                        text = if (state.fullUrlMode) {
                            "当前：直接以 Base URL 发请求"
                        } else {
                            "当前：Base URL + /chat/completions"
                        },
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }

            // API Key with history dropdown (masked)
            HistoryTextField(
                value = state.apiKey,
                onValueChange = viewModel::updateApiKey,
                label = "API Key",
                placeholder = if (state.isNew) "粘贴你的 API Key" else "留空则保留原 Key",
                history = state.apiKeyHistory,
                keyboardType = KeyboardType.Password,
                mask = true
            )

            // Model name dropdown — shows supplier's suggested models, but allows free input
            ModelSelector(
                value = state.modelName,
                suggestions = state.suppliers.firstOrNull { it.id == state.supplierId }?.suggestedModels ?: emptyList(),
                onValueChange = viewModel::updateModel
            )

            Row(verticalAlignment = Alignment.CenterVertically) {
                Checkbox(
                    checked = false,
                    onCheckedChange = null,
                    enabled = false
                )
                Text(
                    text = "保存后可在列表中点按激活",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }

            Spacer(Modifier.height(8.dp))

            Button(
                onClick = {
                    viewModel.save(makeDefault = false) { ok ->
                        if (ok) onSaved()
                    }
                },
                modifier = Modifier.fillMaxWidth()
            ) {
                Text("保存")
            }

            state.error?.let { err ->
                Text(
                    text = err,
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall
                )
            }

            if (state.saved) {
                LaunchedEffect(Unit) { onSaved() }
            }
        }
    }
}

/**
 * OutlinedTextField with a trailing 📋 (history) icon that opens a dropdown
 * of previously-stored values for one-tap prefill.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun HistoryTextField(
    value: String,
    onValueChange: (String) -> Unit,
    label: String,
    placeholder: String,
    history: List<String>,
    keyboardType: KeyboardType,
    mask: Boolean
) {
    var expanded by remember { mutableStateOf(false) }
    var revealed by remember { mutableStateOf(false) }

    ExposedDropdownMenuBox(
        expanded = expanded,
        onExpandedChange = { expanded = it }
    ) {
        OutlinedTextField(
            value = value,
            onValueChange = onValueChange,
            label = { Text(label) },
            placeholder = { Text(placeholder) },
            singleLine = true,
            visualTransformation = when {
                !mask -> VisualTransformation.None
                revealed -> VisualTransformation.None
                else -> PasswordVisualTransformation()
            },
            keyboardOptions = KeyboardOptions(keyboardType = keyboardType),
            trailingIcon = {
                Row {
                    if (mask && value.isNotEmpty()) {
                        IconButton(onClick = { revealed = !revealed }) {
                            Text(if (revealed) "🙈" else "👁", style = MaterialTheme.typography.bodyMedium)
                        }
                    }
                    if (history.isNotEmpty()) {
                        IconButton(onClick = { expanded = !expanded }) {
                            Icon(
                                imageVector = Icons.Filled.History,
                                contentDescription = "历史记录"
                            )
                        }
                    }
                }
            },
            modifier = Modifier
                .fillMaxWidth()
                .menuAnchor()
        )
        DropdownMenu(
            expanded = expanded,
            onDismissRequest = { expanded = false }
        ) {
            if (history.isEmpty()) {
                DropdownMenuItem(
                    text = { Text("暂无历史") },
                    onClick = { expanded = false }
                )
            } else {
                history.forEach { item ->
                    DropdownMenuItem(
                        text = { Text(if (mask) maskKey(item) else item) },
                        onClick = {
                            onValueChange(item)
                            expanded = false
                        }
                    )
                }
            }
        }
    }
}

private fun maskKey(key: String): String {
    if (key.length <= 8) return "••••"
    return key.take(4) + "••••" + key.takeLast(4)
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SupplierSelector(
    selectedId: String,
    suppliers: List<Supplier>,
    onSelect: (String) -> Unit
) {
    var expanded by remember { mutableStateOf(false) }
    val selected = suppliers.firstOrNull { it.id == selectedId }

    ExposedDropdownMenuBox(
        expanded = expanded,
        onExpandedChange = { expanded = !expanded }
    ) {
        OutlinedTextField(
            value = selected?.displayName ?: "",
            onValueChange = {},
            readOnly = true,
            label = { Text("供应商") },
            trailingIcon = {
                ExposedDropdownMenuDefaults.TrailingIcon(expanded = expanded)
            },
            modifier = Modifier
                .fillMaxWidth()
                .menuAnchor()
        )
        DropdownMenu(
            expanded = expanded,
            onDismissRequest = { expanded = false }
        ) {
            suppliers.forEach { supplier ->
                DropdownMenuItem(
                    text = { Text(supplier.displayName) },
                    onClick = {
                        onSelect(supplier.id)
                        expanded = false
                    }
                )
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ModelSelector(
    value: String,
    suggestions: List<String>,
    onValueChange: (String) -> Unit
) {
    var expanded by remember { mutableStateOf(false) }

    ExposedDropdownMenuBox(
        expanded = expanded,
        onExpandedChange = { expanded = !expanded }
    ) {
        OutlinedTextField(
            value = value,
            onValueChange = onValueChange,
            label = { Text("模型名称") },
            placeholder = { Text("如 gpt-4o-mini / deepseek-chat") },
            singleLine = true,
            trailingIcon = {
                if (suggestions.isNotEmpty()) {
                    IconButton(onClick = { expanded = !expanded }) {
                        Icon(Icons.Filled.ArrowDropDown, contentDescription = "推荐模型")
                    }
                }
            },
            modifier = Modifier
                .fillMaxWidth()
                .menuAnchor()
        )
        if (suggestions.isNotEmpty()) {
            DropdownMenu(
                expanded = expanded,
                onDismissRequest = { expanded = false }
            ) {
                suggestions.forEach { model ->
                    DropdownMenuItem(
                        text = { Text(model) },
                        onClick = {
                            onValueChange(model)
                            expanded = false
                        }
                    )
                }
            }
        }
    }
}
