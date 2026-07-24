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
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
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
import androidx.compose.material.ripple.rememberRipple
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextField
import androidx.compose.material3.TextFieldDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import com.example.aichat.data.attachment.AttachmentSource
import com.example.aichat.data.attachment.AttachmentSourceRegistry
import com.example.aichat.ui.chat.model.Attachment

/**
 * Floating input bar — Kimi / iMessage style.
 *
 * Layout:
 *   - No outer container: the bar floats over the chat list with transparent
 *     background (Scaffold's bottomBar slot is intentionally left empty so
 *     no divider line is drawn).
 *   - The whole pill (attachments preview + TextField + inline buttons) is a
 *     single [Surface] with [surfaceVariant] fill + 24dp corner radius.
 *   - ⊕ (add attachment) sits as the TextField's leading icon; Stop / Mic /
 *     Send sit as the trailing icon. They never overflow below the pill.
 *
 * Button state machine (trailing):
 *   - isLoading → ⏹ Stop
 *   - input empty AND no attachments AND voice available → 🎤 Mic
 *       (press-and-hold via [InteractionSource.collectIsPressedAsState] —
 *        more reliable than detectTapGestures; release auto-sends)
 *   - otherwise → ➤ Send
 *
 * Keyboard: [Modifier.imePadding] + [Modifier.navigationBarsPadding] keep
 * the pill above the IME.
 */
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

    var pendingCameraUri by remember { mutableStateOf<Uri?>(null) }

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

    val cameraPermissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        hasCameraPermission = granted
        if (granted) {
            val uri = CaptureUriProvider.newImageUri(context)
            pendingCameraUri = uri
            cameraLauncher.launch(uri)
        }
    }
    val micPermissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted -> hasMicPermission = granted }

    // Voice recognizer — release auto-sends transcribed text
    val voice = rememberVoiceRecognizer(onResult = { recognized ->
        val clean = recognized.trim()
        if (clean.isNotEmpty() || pendingAttachments.isNotEmpty()) {
            onSend(clean, pendingAttachments)
            text = ""
            keyboard?.hide()
        }
    })

    // ── Pill container ─────────────────────────────────────────────────────
    Surface(
        modifier = modifier
            .fillMaxWidth()
            .navigationBarsPadding()
            .imePadding(),
        shape = RoundedCornerShape(28.dp),
        color = MaterialTheme.colorScheme.surfaceVariant,
        // No tonalElevation → no divider/shadow line, pill looks floating
        tonalElevation = 0.dp,
        shadowElevation = 0.dp
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 4.dp, vertical = 4.dp)
        ) {
            // Attachment previews (inside the pill)
            if (pendingAttachments.isNotEmpty()) {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .horizontalScroll(rememberScrollState())
                        .padding(start = 8.dp, end = 8.dp, top = 6.dp, bottom = 2.dp),
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

            // Input row — TextField with embedded leading (⊕) + trailing (Stop/Mic/Send) icons
            TextField(
                value = text,
                onValueChange = { text = it },
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(min = 48.dp, max = 144.dp),
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
                shape = RoundedCornerShape(24.dp),
                leadingIcon = {
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
                },
                trailingIcon = {
                    when {
                        isLoading -> {
                            Icon(
                                imageVector = Icons.Filled.StopCircle,
                                contentDescription = "停止生成",
                                tint = MaterialTheme.colorScheme.error,
                                modifier = Modifier
                                    .size(40.dp)
                                    .clip(RoundedCornerShape(percent = 50))
                                    .clickable(onClick = onStop)
                                    .padding(8.dp)
                            )
                        }
                        text.isBlank() && pendingAttachments.isEmpty() && voice.isAvailable -> {
                            MicButton(
                                active = voice.isListening.value,
                                enabled = !isLoading,
                                modifier = Modifier.size(48.dp),
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
                            Icon(
                                imageVector = Icons.AutoMirrored.Filled.Send,
                                contentDescription = "发送",
                                tint = if (canSend) MaterialTheme.colorScheme.primary
                                       else MaterialTheme.colorScheme.outline,
                                modifier = Modifier
                                    .size(40.dp)
                                    .clip(RoundedCornerShape(percent = 50))
                                    .clickable(enabled = canSend) {
                                        if (canSend) {
                                            onSend(text.trim(), pendingAttachments)
                                            text = ""
                                            keyboard?.hide()
                                        }
                                    }
                                    .padding(8.dp)
                            )
                        }
                    }
                },
                colors = TextFieldDefaults.colors(
                    focusedContainerColor = Color.Transparent,
                    unfocusedContainerColor = Color.Transparent,
                    disabledContainerColor = Color.Transparent,
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

/**
 * Press-and-hold Mic button.
 *
 * Uses [MutableInteractionSource.collectIsPressedAsState] instead of
 * `detectTapGestures(onPress = ...)`. The InteractionSource path is the
 * officially recommended way for press-and-hold in Compose and survives
 * recomposition + configuration changes more reliably.
 *
 * While pressed → onStart(). On release → onStop(). The visual ripple +
 * pulse animation gives immediate feedback even if ASR hasn't received
 * audio yet.
 */
@Composable
private fun MicButton(
    active: Boolean,
    enabled: Boolean,
    modifier: Modifier = Modifier,
    onPress: () -> Unit,
    onRelease: () -> Unit
) {
    val interactionSource = remember { MutableInteractionSource() }
    val isPressed by interactionSource.collectIsPressedAsState()

    // Drive the recognizer start/stop from the pressed state
    LaunchedEffect(isPressed) {
        if (!enabled) return@LaunchedEffect
        if (isPressed) onPress() else onRelease()
    }

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
                if (active) MaterialTheme.colorScheme.error.copy(alpha = 0.18f)
                else Color.Transparent
            )
            .clickable(
                interactionSource = interactionSource,
                indication = rememberRipple(bounded = false, radius = 24.dp),
                enabled = enabled,
                onClick = { /* press handled via interactionSource */ }
            ),
        contentAlignment = Alignment.Center
    ) {
        Icon(
            imageVector = Icons.Filled.Mic,
            contentDescription = if (active) "正在聆听..." else "按住说话",
            tint = if (active) MaterialTheme.colorScheme.error
                   else MaterialTheme.colorScheme.primary,
            modifier = Modifier
                .alpha(if (active) pulseAlpha else 1f)
                .padding(10.dp)
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
