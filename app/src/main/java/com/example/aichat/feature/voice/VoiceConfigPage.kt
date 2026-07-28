package com.example.aichat.feature.voice

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle

/**
 * v4.2.12 #3b: Settings page for STT + TTS endpoints.
 *
 * Fields are grouped into a "语音识别 (STT)" section and a "语音合成 (TTS)"
 * section. Each section's URL/Key/Model fields are independently optional:
 * leaving STT blank keeps using Android's built-in SpeechRecognizer; leaving
 * TTS blank hides the speaker icon on AI bubbles.
 *
 * The form is local state — nothing is persisted until the user taps
 * "保存", at which point we encrypt + write via [VoiceConfigRepository].
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun VoiceConfigPage(
    onBack: () -> Unit,
    viewModel: VoiceConfigViewModel = hiltViewModel()
) {
    val form by viewModel.form.collectAsStateWithLifecycle()

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("语音服务") },
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
                .padding(16.dp)
                .verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            // ── STT section ────────────────────────────────────────────────
            SectionHeader("语音识别 (STT)")
            HelpText("留空则使用系统 SpeechRecognizer。填写则录音后 POST 到 OpenAI-Whisper 兼容端点（/v1/audio/transcriptions）。")

            OutlinedTextField(
                value = form.sttUrl,
                onValueChange = viewModel::updateSttUrl,
                label = { Text("STT URL") },
                placeholder = { Text("https://api.openai.com/v1/audio/transcriptions") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth()
            )
            SecretTextField(
                value = form.sttApiKey,
                onValueChange = viewModel::updateSttKey,
                label = "STT API Key (可选)"
            )
            OutlinedTextField(
                value = form.sttModel,
                onValueChange = viewModel::updateSttModel,
                label = { Text("STT Model") },
                placeholder = { Text("whisper-1") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth()
            )

            Spacer(Modifier.height(8.dp))
            HorizontalDivider()
            Spacer(Modifier.height(8.dp))

            // ── TTS section ────────────────────────────────────────────────
            SectionHeader("语音合成 (TTS)")
            HelpText("留空则隐藏朗读按钮。填写则点击 AI 气泡的 🎧 调用 OpenAI-TTS 兼容端点（/v1/audio/speech）合成并播放 mp3。")

            OutlinedTextField(
                value = form.ttsUrl,
                onValueChange = viewModel::updateTtsUrl,
                label = { Text("TTS URL") },
                placeholder = { Text("https://api.openai.com/v1/audio/speech") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth()
            )
            SecretTextField(
                value = form.ttsApiKey,
                onValueChange = viewModel::updateTtsKey,
                label = "TTS API Key (可选)"
            )
            OutlinedTextField(
                value = form.ttsModel,
                onValueChange = viewModel::updateTtsModel,
                label = { Text("TTS Model") },
                placeholder = { Text("tts-1") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth()
            )
            OutlinedTextField(
                value = form.ttsVoice,
                onValueChange = viewModel::updateTtsVoice,
                label = { Text("TTS Voice") },
                placeholder = { Text("alloy / echo / fable / onyx / nova / shimmer") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth()
            )

            Spacer(Modifier.height(12.dp))
            Button(
                onClick = { viewModel.save(onBack) },
                modifier = Modifier.fillMaxWidth()
            ) {
                Text("保存")
            }
        }
    }
}

@Composable
private fun SectionHeader(text: String) {
    Text(
        text = text,
        style = MaterialTheme.typography.titleMedium,
        fontWeight = FontWeight.SemiBold,
        color = MaterialTheme.colorScheme.primary
    )
}

@Composable
private fun HelpText(text: String) {
    Text(
        text = text,
        style = MaterialTheme.typography.bodySmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant
    )
}

@Composable
private fun SecretTextField(
    value: String,
    onValueChange: (String) -> Unit,
    label: String
) {
    var visible by androidx.compose.runtime.remember { androidx.compose.runtime.mutableStateOf(false) }
    OutlinedTextField(
        value = value,
        onValueChange = onValueChange,
        label = { Text(label) },
        singleLine = true,
        visualTransformation = if (visible) VisualTransformation.None
                               else PasswordVisualTransformation(),
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
        trailingIcon = {
            IconButton(onClick = { visible = !visible }) {
                Text(if (visible) "隐藏" else "显示", style = MaterialTheme.typography.labelSmall)
            }
        },
        modifier = Modifier.fillMaxWidth()
    )
}
