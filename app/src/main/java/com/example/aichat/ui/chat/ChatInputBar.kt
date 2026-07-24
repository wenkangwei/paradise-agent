package com.example.aichat.ui.chat

import android.Manifest
import android.content.pm.PackageManager
import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.StopCircle
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextField
import androidx.compose.material3.TextFieldDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import com.example.aichat.data.attachment.AttachmentSource
import com.example.aichat.data.attachment.AttachmentSourceRegistry
import com.example.aichat.ui.chat.model.Attachment

/**
 * Kimi-style input bar with three trailing-button states:
 *   - AI is responding → ⏹ Stop
 *   - Input is empty AND no attachments → 🎤 mic (press-and-hold to dictate; release sends)
 *   - Otherwise → ➤ send
 *
 * Attachments: ⊕ opens a bottom sheet (Camera / Gallery / Files). The camera
 * source goes through a real [androidx.core.content.FileProvider] URI so the
 * system camera app writes directly into our cache — no more fallback to gallery.
 *
 * Keyboard: the whole bar uses [Modifier.imePadding] + [Modifier.navigationBarsPadding]
 * so it always sits above the IME (and the keyboard never overlaps the input field).
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun ChatInputBar(
    isLoading: Boolean,
    onSend: (String, List<Attachment>) -> Unit,
    onStop: () -> Unit,
    pendingAttachments: List<Attachment> = emptyList(),
    onAddAttachment: (String, String) -> Unit,
    onRemoveAttachment: (String) -> Unit,
    modifier: Modifier = Modifier
) {
    var text by rememberSaveable { mutableStateOf("") }
    var showSheet by rememberSaveable { mutableStateOf(false) }
    val keyboard = LocalSoftwareKeyboardController.current
    val context = LocalContext.current

    // Camera capture URI is held transiently between launching TakePicture and the result callback
    var pendingCameraUri by remember { mutableStateOf<Uri?>(null) }

    // Runtime permissions for camera + microphone
    var hasCameraPermission by remember {
        mutableStateOf(
            ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED
        )
    }
    var hasMicPermission by remember {
        mutableStateOf(
            ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        )
    }

    val galleryLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.PickVisualMedia()
    ) { uri -> if (uri != null) onAddAttachment(uri.toString(), guessMime(uri)) }

    val fileLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.GetContent()
    ) { uri -> if (uri != null) onAddAttachment(uri.toString(), guessMime(uri)) }

    val cameraLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.TakePicture()
    ) { ok ->
        val captured = pendingCameraUri
        if (ok && captured != null) onAddAttachment(captured.toString(), "image/jpeg")
        pendingCameraUri = null
    }

    // Permission launchers — declared AFTER cameraLauncher so the callback can re-enter it
    val cameraPermissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        hasCameraPermission = granted
        if (granted) {
            // User just approved camera permission — open system camera now
            val uri = CaptureUriProvider.newImageUri(context)
            pendingCameraUri = uri
            cameraLauncher.launch(uri)
        }
    }
    val micPermissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted -> hasMicPermission = granted }

    // ── Voice recognizer (release-to-send) ────────────────────────────────
    val voice = rememberVoiceRecognizer(onResult = { recognized ->
        // Auto-send transcribed text + any pending attachments
        val clean = recognized.trim()
        if (clean.isNotEmpty() || pendingAttachments.isNotEmpty()) {
            onSend(clean, pendingAttachments)
            text = ""
            keyboard?.hide()
        }
    })

    Surface(
        modifier = modifier
            .fillMaxWidth()
            .navigationBarsPadding()
            .imePadding(),
        tonalElevation = 2.dp,
        color = MaterialTheme.colorScheme.surface
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 12.dp, vertical = 8.dp)
        ) {
            // 1) Pending attachments preview
            if (pendingAttachments.isNotEmpty()) {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .horizontalScroll(rememberScrollState())
                        .padding(bottom = 4.dp),
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    pendingAttachments.forEach { attachment ->
                        AttachmentPreview(
                            attachment = attachment,
                            onRemove = { onRemoveAttachment(attachment.id) }
                        )
                    }
                }
            }

            // 2) Functional row: ⊕ + input + trailing button
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.Bottom
            ) {
                // ⊕ attachment button
                Box(
                    modifier = Modifier
                        .size(40.dp)
                        .clip(RoundedCornerShape(percent = 50))
                        .clickable(enabled = !isLoading) { showSheet = true },
                    contentAlignment = Alignment.Center
                ) {
                    Icon(
                        imageVector = Icons.Filled.Add,
                        contentDescription = "添加附件",
                        tint = if (isLoading) MaterialTheme.colorScheme.outline
                               else MaterialTheme.colorScheme.primary
                    )
                }

                Spacer(Modifier.width(8.dp))

                // Multi-line input — capsule shape
                TextField(
                    value = text,
                    onValueChange = { text = it },
                    modifier = Modifier
                        .weight(1f)
                        .heightIn(min = 40.dp, max = 144.dp),
                    placeholder = {
                        val hint = when {
                            isLoading -> "AI 正在回复..."
                            voice.isAvailable && text.isBlank() -> "说点什么，或按住 🎤 说话"
                            else -> "说点什么吧..."
                        }
                        Text(text = hint)
                    },
                    enabled = !isLoading,
                    maxLines = 5,
                    shape = RoundedCornerShape(20.dp),
                    colors = TextFieldDefaults.colors(
                        focusedContainerColor = MaterialTheme.colorScheme.surfaceVariant,
                        unfocusedContainerColor = MaterialTheme.colorScheme.surfaceVariant,
                        disabledContainerColor = MaterialTheme.colorScheme.surfaceVariant,
                        focusedIndicatorColor = Color.Transparent,
                        unfocusedIndicatorColor = Color.Transparent,
                        disabledIndicatorColor = Color.Transparent
                    ),
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Default),
                    keyboardActions = KeyboardActions(onSend = {
                        val trimmed = text.trim()
                        if (trimmed.isNotEmpty() || pendingAttachments.isNotEmpty()) {
                            onSend(trimmed, pendingAttachments)
                            text = ""
                            keyboard?.hide()
                        }
                    })
                )

                Spacer(Modifier.width(8.dp))

                // Trailing button: Stop | Mic | Send
                when {
                    isLoading -> {
                        IconButton(onClick = onStop, modifier = Modifier.size(40.dp)) {
                            Icon(
                                imageVector = Icons.Filled.StopCircle,
                                contentDescription = "停止生成",
                                tint = MaterialTheme.colorScheme.error
                            )
                        }
                    }
                    text.isBlank() && pendingAttachments.isEmpty() && voice.isAvailable -> {
                        // Mic button — press-and-hold to dictate; release auto-sends
                        val isListening = voice.isListening.value
                        MicButton(
                            active = isListening,
                            enabled = !isLoading,
                            modifier = Modifier.size(40.dp),
                            onPress = {
                                if (!hasMicPermission) {
                                    micPermissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
                                } else {
                                    voice.start()
                                }
                            },
                            onRelease = { voice.stop() }
                        )
                    }
                    else -> {
                        val canSend = text.isNotBlank() || pendingAttachments.isNotEmpty()
                        IconButton(
                            onClick = {
                                if (canSend) {
                                    onSend(text.trim(), pendingAttachments)
                                    text = ""
                                    keyboard?.hide()
                                }
                            },
                            enabled = canSend,
                            modifier = Modifier.size(40.dp)
                        ) {
                            Icon(
                                imageVector = Icons.AutoMirrored.Filled.Send,
                                contentDescription = "发送",
                                tint = if (canSend) MaterialTheme.colorScheme.primary
                                       else MaterialTheme.colorScheme.outline
                            )
                        }
                    }
                }
            }
        }

        if (showSheet) {
            AttachmentSheet(
                sources = AttachmentSourceRegistry().all,
                onPick = { source ->
                    showSheet = false
                    when (source) {
                        AttachmentSource.Camera -> {
                            if (!hasCameraPermission) {
                                cameraPermissionLauncher.launch(Manifest.permission.CAMERA)
                            } else {
                                val uri = CaptureUriProvider.newImageUri(context)
                                pendingCameraUri = uri
                                cameraLauncher.launch(uri)
                            }
                        }
                        AttachmentSource.Gallery -> galleryLauncher.launch(
                            PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageAndVideo)
                        )
                        AttachmentSource.Files -> fileLauncher.launch("*/*")
                    }
                },
                onDismiss = { showSheet = false }
            )
        }
    }
}

/** Standalone helper (kept for symmetry with permission callback path). */
private fun launchCamera(
    context: android.content.Context,
    setUri: (Uri?) -> Unit,
    setRef: (Uri?) -> Unit,
    @Suppress("UNUSED_PARAMETER") noop: () -> Unit
) {
    val uri = CaptureUriProvider.newImageUri(context)
    setUri(uri); setRef(uri)
}

/** Mic icon that pulses while actively listening. Uses press-and-hold via pointerInput. */
@Composable
private fun MicButton(
    active: Boolean,
    enabled: Boolean,
    modifier: Modifier = Modifier,
    onPress: () -> Unit,
    onRelease: () -> Unit
) {
    val transition = rememberInfiniteTransition(label = "mic")
    val pulseAlpha by transition.animateFloat(
        initialValue = 1f,
        targetValue = 0.3f,
        animationSpec = infiniteRepeatable(tween(500), RepeatMode.Reverse),
        label = "micAlpha"
    )

    Box(
        modifier = modifier
            .clip(RoundedCornerShape(percent = 50))
            .background(
                if (active) MaterialTheme.colorScheme.error.copy(alpha = 0.15f)
                else Color.Transparent
            )
            .pointerInput(enabled) {
                if (!enabled) return@pointerInput
                detectTapGestures(
                    onPress = {
                        onPress()
                        tryAwaitRelease()
                        onRelease()
                    }
                )
            },
        contentAlignment = Alignment.Center
    ) {
        Icon(
            imageVector = Icons.Filled.Mic,
            contentDescription = if (active) "正在聆听..." else "按住说话",
            tint = if (active) MaterialTheme.colorScheme.error
                   else MaterialTheme.colorScheme.primary,
            modifier = Modifier.alpha(if (active) pulseAlpha else 1f)
        )
    }
}

/** Guess a MIME type from a URI when the picker doesn't provide one. */
private fun guessMime(uri: Uri): String {
    val ext = uri.lastPathSegment?.substringAfterLast('.', missingDelimiterValue = "").orEmpty()
    return when (ext.lowercase()) {
        "png" -> "image/png"
        "jpg", "jpeg" -> "image/jpeg"
        "gif" -> "image/gif"
        "webp" -> "image/webp"
        "pdf" -> "application/pdf"
        "txt" -> "text/plain"
        "json" -> "application/json"
        "mp4" -> "video/mp4"
        "mp3" -> "audio/mpeg"
        "doc" -> "application/msword"
        "docx" -> "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        "xls" -> "application/vnd.ms-excel"
        "xlsx" -> "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        "ppt" -> "application/vnd.ms-powerpoint"
        "pptx" -> "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        "csv" -> "text/csv"
        else -> "application/octet-stream"
    }
}
