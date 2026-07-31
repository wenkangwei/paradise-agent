package com.example.aichat.ui.chat

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.speech.RecognizerIntent
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.compose.runtime.rememberCoroutineScope
import androidx.core.content.FileProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import java.io.File
import java.util.concurrent.TimeUnit
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
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
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Keyboard
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.StopCircle
import androidx.compose.material.ripple.rememberRipple
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextField
import androidx.compose.material3.TextFieldDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.input.pointer.changedToUp
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalHapticFeedback
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
 *
 * v4.2.12 #3a-redesign: two modes toggled by tapping the mic / keyboard icon.
 *   - TEXT mode (default): ⊕ leading, TextField body, trailing = Stop / Mic /
 *     Send. Tapping Mic (when input is empty + voice available) flips to VOICE.
 *   - VOICE mode: ⊕ + 按住 说话 (press-and-hold) + ⌨. Long-pressing the middle
 *     button drives the SpeechRecognizer; releasing transcribes and sends.
 *     Tapping ⌨ flips back to TEXT.
 *
 * Button state machine (trailing, TEXT mode):
 *   - isLoading → ⏹ Stop
 *   - voice available → 🎤 Mic (tap to enter VOICE mode)
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
    serverBaseUrl: String = "",
    /**
     * One-shot external prefill. When it goes non-null the bar replaces its
     * current text with this value, brings up the IME, and immediately calls
     * [onPendingInputConsumed] so the caller can clear the slot. Used for
     * "tap a favorite tool → load into input box" (see ChatViewModel.useFavoriteTool).
     */
    pendingInput: String? = null,
    onPendingInputConsumed: () -> Unit = {},
    modifier: Modifier = Modifier
) {
    var text by rememberSaveable { mutableStateOf("") }
    var showSheet by rememberSaveable { mutableStateOf(false) }
    // v4.2.12 #3a-redesign: Kimi-style mode toggle. Click 🎤 → voice mode
    // (TextField replaced by 按住说话 button). Click ⌨ → back to text mode.
    // Long-press in voice mode records; release transcribes and sends.
    var inputMode by rememberSaveable { mutableStateOf(InputMode.TEXT) }
    val keyboard = LocalSoftwareKeyboardController.current
    val context = LocalContext.current

    // Drain any external prefill exactly once per non-null emission.
    LaunchedEffect(pendingInput) {
        if (pendingInput != null) {
            text = pendingInput
            onPendingInputConsumed()
            // Don't auto-send — let the user edit / review first.
            keyboard?.show()
        }
    }

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
    ) { uri ->
        if (uri != null) {
            val mime = context.contentResolver.getType(uri) ?: guessMime(uri)
            onAddAttachment(uri.toString(), mime)
        }
    }

    val fileLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.GetContent()
    ) { uri ->
        if (uri != null) {
            val mime = context.contentResolver.getType(uri) ?: guessMime(uri)
            onAddAttachment(uri.toString(), mime)
        }
    }

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

    // Voice: record WAV → upload to server Whisper → send text
    // Vosk code preserved in VoiceInput as fallback
    val scope = rememberCoroutineScope()
    val voiceRecorder = rememberVoiceRecorder(context = context,
        onResult = { result ->
            scope.launch {
                // Try server Whisper first, fallback to Vosk
                val transcribed = withContext(Dispatchers.IO) {
                    val whisperText = uploadForStt(result.wavFile, serverBaseUrl)
                    if (whisperText.isNotEmpty()) whisperText
                    else if (result.transcript.isNotEmpty()) result.transcript
                    else ""
                }
                if (transcribed.isNotEmpty()) {
                    onSend(transcribed, pendingAttachments)
                    text = ""
                    keyboard?.hide()
                }
                inputMode = InputMode.TEXT
            }
        }
    )

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
            // v4.2.12 #3a-redesign: in TEXT mode this Column holds the
            // attachment preview row + TextField; in VOICE mode it holds
            // the attachment preview row + HoldToSpeakButton. The mode
            // toggle is implemented inline below — see the `if (inputMode)`
            // branch.

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

            // v4.2.12 #3a-redesign: Kimi-style mode toggle. TEXT mode shows the
            // TextField with ⊕ leading and Stop/Mic/Send trailing. VOICE mode
            // swaps the entire row for a wide "按住 说话" hold-button + keyboard
            // toggle. This avoids the layout-thrashing flicker we hit when the
            // voice status bar appeared/disappeared inside the same Surface as
            // the press-and-hold MicButton (the press kept getting cancelled
            // because the button's screen position shifted on every state
            // change). In the new design, pressing happens on a stable full-
            // width button whose bounds never move during a session.
            if (inputMode == InputMode.TEXT) {
                TextField(
                    value = text,
                    onValueChange = { text = it },
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = 48.dp, max = 144.dp),
                    placeholder = {
                        val hint = when {
                            isLoading -> "AI 正在回复..."
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
                            // v4.2.12 #3a-redesign: Mic shows only when the
                            // input box is EMPTY. As soon as the user types a
                            // single character (or attaches anything) the slot
                            // flips to Send — same UX as the pre-redesign
                            // behavior. Tapping Mic flips the whole row into
                            // VOICE mode (HoldToSpeakButton); long-press-to-
                            // record happens there, not here.
                            text.isBlank() && pendingAttachments.isEmpty() && voiceRecorder.isAvailable -> {
                                Icon(
                                    imageVector = Icons.Filled.Mic,
                                    contentDescription = "切换到语音模式",
                                    tint = MaterialTheme.colorScheme.primary,
                                    modifier = Modifier
                                        .size(40.dp)
                                        .clip(RoundedCornerShape(percent = 50))
                                        .clickable(enabled = !isLoading) {
                                            if (!hasMicPermission) {
                                                micPermissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
                                            }
                                            inputMode = InputMode.VOICE
                                        }
                                        .padding(8.dp)
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
            } else {
                // VOICE mode: ⊕ stays on the left so the user can still attach
                // images/files; the hold-to-speak button takes the middle; a
                // keyboard icon on the right toggles back to TEXT mode.
                // Voice mode: press-and-hold triggers Android SpeechRecognizer.
                // onPressStart/onPressEnd wire to VoiceRecognizer.start()/stop().
                // Recognized text is sent via rememberVoiceRecognizer's onResult
                // callback (declared above, line 191).
                HoldToSpeakButton(
                    isLoading = isLoading,
                    enabled = !isLoading,
                    onPressStart = { voiceRecorder.start() },
                    onPressEnd = { voiceRecorder.stop() },
                    onAddAttachment = { showSheet = true },
                    onSwitchToText = { inputMode = InputMode.TEXT },
                    onStop = onStop,
                    onSend = { /* sent via voice.onResult callback */ },
                    onCancel = { /* user cancelled — voice already stopped */ }
                )
            }
        }
    }

    // v4.2.12 #3a-redesign: voice state (listening / transcribing / waveform)
    // lives INSIDE HoldToSpeakButton — see the VOICE branch above. No Popup,
    // no sibling status strip; the button's bounds are fixed by the row
    // layout so the press-and-hold never gets cancelled by reflow.

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
                    AttachmentSource.Ocr -> galleryLauncher.launch(
                        PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)
                    )
                    AttachmentSource.ObjectDetection -> galleryLauncher.launch(
                        PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)
                    )
                }
            },
            onDismiss = { showSheet = false }
        )
    }
}

/**
 * v4.2.12 #3a-redesign: Kimi-style voice-mode input row.
 *
 * Layout:
 *   [⊕ attach]  [   hold-to-speak button (weight=1, fixed height)   ]  [⌨ keyboard]
 *
 * Gesture-driven state machine — UI state is owned entirely by the
 * gesture handler, NOT by any recognizer callback. This breaks the
 * flicker cycle: previously the UI was driven by `voice.isListening /
 * isTranscribing`, which Honor's flaky SpeechRecognizer would set/clear
 * asynchronously and out of step with the user's finger, causing the
 * press to be cancelled and the state to oscillate.
 *
 * State transitions:
 *   press down           : IDLE      → RECORDING
 *   drag outside bounds  : RECORDING → CANCELING
 *   drag back inside     : CANCELING → RECORDING
 *   release in RECORDING : SEND + IDLE
 *   release in CANCELING : IDLE (no send)
 *
 * Layout invariant: the pressable Box uses fixed `height(48.dp)`, NOT
 * `heightIn(min = ...)`. The content swap (icon vs waveform vs text)
 * therefore never changes the Box's outer bounds → no reflow → the touch
 * position stays under the finger → press is never cancelled by layout.
 *
 * v4.2.12 #3a-test: onSend sends a hardcoded "测试" string. Real ASR
 * integration (whisper-compatible HTTP endpoint or system ASR) comes in
 * a follow-up — see plan §3a.
 */
@Composable
private fun HoldToSpeakButton(
    isLoading: Boolean,
    enabled: Boolean,
    onPressStart: () -> Unit,
    onPressEnd: () -> Unit,
    onSend: () -> Unit,
    onCancel: () -> Unit,
    onAddAttachment: () -> Unit,
    onSwitchToText: () -> Unit,
    onStop: () -> Unit,
    modifier: Modifier = Modifier
) {
    // Single source of truth for the button's UI. Driven ONLY by gesture
    // events below — never by any external state (recognizer callbacks,
    // StateFlow, etc.). This is what kills the v4.2.12 #3a flicker.
    var gestureState by remember { mutableStateOf(GestureState.IDLE) }

    // v4.2.12 #3a-haptic: every gesture-state transition buzzes the device
    // so the user feels the state change without looking at the screen.
    //   IDLE      → RECORDING   : LongPress  (heavy, "recording started")
    //   RECORDING → CANCELING   : light tick ("oops, you left the zone")
    //   CANCELING → RECORDING   : light tick ("ok, back on")
    //   release (either side)   : LongPress  (heavy, "done")
    // Mirrors Kimi/WeChat's voice-mode haptic semantics.
    val haptic = LocalHapticFeedback.current
    fun transitionTo(next: GestureState) {
        if (next == gestureState) return
        val type = when (next) {
            GestureState.RECORDING -> HapticFeedbackType.LongPress
            GestureState.CANCELING -> HapticFeedbackType.TextHandleMove
            GestureState.IDLE -> HapticFeedbackType.LongPress
        }
        haptic.performHapticFeedback(type)
        gestureState = next
    }

    Row(
        modifier = modifier
            .fillMaxWidth()
            .height(48.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        // ⊕ — same add-attachment affordance as TEXT mode
        Box(
            modifier = Modifier
                .size(40.dp)
                .clip(RoundedCornerShape(percent = 50))
                .clickable(enabled = enabled, onClick = onAddAttachment),
            contentAlignment = Alignment.Center
        ) {
            Icon(
                imageVector = Icons.Filled.Add,
                contentDescription = "添加附件",
                tint = if (enabled) MaterialTheme.colorScheme.primary
                       else MaterialTheme.colorScheme.outline
            )
        }

        // Big hold-to-speak button. Fixed 48.dp height is critical — see
        // the doc above. Content swaps inside never affect outer bounds.
        Box(
            modifier = Modifier
                .weight(1f)
                .height(48.dp)
                .clip(RoundedCornerShape(24.dp))
                .background(
                    when (gestureState) {
                        GestureState.RECORDING -> MaterialTheme.colorScheme.error.copy(alpha = 0.10f)
                        GestureState.CANCELING -> MaterialTheme.colorScheme.error.copy(alpha = 0.22f)
                        GestureState.IDLE -> MaterialTheme.colorScheme.surface
                    }
                )
                .pointerInput(enabled) {
                    if (!enabled) return@pointerInput
                    awaitEachGesture {
                        // Finger down — enter RECORDING immediately. This is
                        // a DOWN event, not a tap; we don't consume it so
                        // the parent's drag detection still works.
                        awaitFirstDown(requireUnconsumed = false)
                        transitionTo(GestureState.RECORDING)
                        onPressStart()

                        // Loop over MOVE/UP events until the gesture ends.
                        // For each event we check whether the finger is
                        // still inside the button's bounds and transition
                        // RECORDING ↔ CANCELING accordingly.
                        try {
                            while (true) {
                                val event = awaitPointerEvent()
                                val change = event.changes.first()
                                val inside = change.position.x >= 0f &&
                                             change.position.x <= size.width.toFloat() &&
                                             change.position.y >= 0f &&
                                             change.position.y <= size.height.toFloat()

                                if (change.changedToUp()) {
                                    // Release — fire callback based on where
                                    // the finger was when released.
                                    val wasRecording = gestureState == GestureState.RECORDING
                                    transitionTo(GestureState.IDLE)
                                    onPressEnd()
                                    if (wasRecording) onSend() else onCancel()
                                    break
                                }

                                val next = if (inside) GestureState.RECORDING
                                           else GestureState.CANCELING
                                if (next != gestureState) transitionTo(next)
                            }
                        } catch (_: Exception) {
                            // Defensive: if the gesture stream aborts
                            // unexpectedly (e.g. parent recomposition yanks
                            // the pointerInput scope), reset to IDLE so we
                            // never get stuck showing "识别中".
                            transitionTo(GestureState.IDLE)
                        }
                    }
                },
            contentAlignment = Alignment.Center
        ) {
            when (gestureState) {
                GestureState.IDLE -> Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    Icon(
                        imageVector = Icons.Filled.Mic,
                        contentDescription = null,
                        tint = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.size(18.dp)
                    )
                    Text(
                        text = "按住 说话",
                        style = MaterialTheme.typography.labelLarge,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }

                GestureState.RECORDING -> Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(10.dp),
                    modifier = Modifier.padding(horizontal = 12.dp)
                ) {
                    // TEST MODE: pass a constant rmsLevel so the existing
                    // WaveformBars animation (which adds per-bar jitter)
                    // produces lively visuals without a live mic feed.
                    WaveformBars(
                        rmsLevel = 0.6f,
                        modifier = Modifier
                            .width(60.dp)
                            .height(22.dp)
                    )
                    Text(
                        text = "识别中 松开发送",
                        style = MaterialTheme.typography.labelLarge,
                        color = MaterialTheme.colorScheme.error
                    )
                }

                GestureState.CANCELING -> Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    Icon(
                        imageVector = Icons.Filled.Close,
                        contentDescription = null,
                        tint = MaterialTheme.colorScheme.error,
                        modifier = Modifier.size(18.dp)
                    )
                    Text(
                        text = "松开 取消",
                        style = MaterialTheme.typography.labelLarge,
                        color = MaterialTheme.colorScheme.error
                    )
                }
            }
        }

        // Trailing slot: ⏹ while AI is replying (so the user can interrupt
        // the generation without leaving voice mode), otherwise ⌨ to flip
        // back to TEXT mode. Mirrors the TEXT-mode trailing slot's behavior
        // of swapping Stop for Mic/Send during loading.
        Box(
            modifier = Modifier
                .size(40.dp)
                .clip(RoundedCornerShape(percent = 50))
                .clickable(enabled = isLoading || enabled, onClick = if (isLoading) onStop else onSwitchToText),
            contentAlignment = Alignment.Center
        ) {
            if (isLoading) {
                Icon(
                    imageVector = Icons.Filled.StopCircle,
                    contentDescription = "停止生成",
                    tint = MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(8.dp)
                )
            } else {
                Icon(
                    imageVector = Icons.Filled.Keyboard,
                    contentDescription = "切换到文本输入",
                    tint = MaterialTheme.colorScheme.primary
                )
            }
        }
    }
}

/**
 * v4.2.12 #3a-redesign: gesture-driven UI state for HoldToSpeakButton.
 * Kept separate from [InputMode] (which is the outer TEXT/VOICE toggle)
 * because the gesture state only applies while in VOICE mode.
 */
private enum class GestureState { IDLE, RECORDING, CANCELING }

/** Guess a MIME type from a URI when the picker doesn't provide one. */
private fun guessMime(uri: Uri): String {
    val ext = uri.lastPathSegment?.substringAfterLast('.', missingDelimiterValue = "").orEmpty()
    return when (ext.lowercase()) {
        "png" -> "image/png"
        "jpg", "jpeg" -> "image/jpeg"
        "gif" -> "image/gif"
        "webp" -> "image/webp"
        "bmp" -> "image/bmp"
        "heic", "heif" -> "image/heic"
        "svg" -> "image/svg+xml"
        "pdf" -> "application/pdf"
        "txt", "log", "ini", "cfg" -> "text/plain"
        "md" -> "text/markdown"
        "json" -> "application/json"
        "xml" -> "application/xml"
        "csv" -> "text/csv"
        "mp4", "m4v" -> "video/mp4"
        "mkv" -> "video/x-matroska"
        "webm" -> "video/webm"
        "mov" -> "video/quicktime"
        "mp3" -> "audio/mpeg"
        "flac" -> "audio/flac"
        "wav" -> "audio/wav"
        "aac" -> "audio/aac"
        "ogg" -> "audio/ogg"
        "m4a" -> "audio/mp4"
        "doc" -> "application/msword"
        "docx" -> "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        "xls" -> "application/vnd.ms-excel"
        "xlsx" -> "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        "ppt" -> "application/vnd.ms-powerpoint"
        "pptx" -> "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        "zip" -> "application/zip"
        "rar" -> "application/x-rar-compressed"
        "7z" -> "application/x-7z-compressed"
        "tar" -> "application/x-tar"
        "gz" -> "application/gzip"
        else -> "application/octet-stream"
    }
}

/**
 * v4.2.12 #3a: Live audio waveform shown in the input bar placeholder while
 * the user is pressing the mic. Heights driven by [rmsLevel] (0..1) reported
 * by SpeechRecognizer.onRmsChanged.
 *
 * Implementation: 7 vertical bars drawn on a Canvas. The center bar uses the
 * raw rmsLevel; neighbors decay symmetrically (multiplying by a fixed shape
 * vector) so the waveform visually centers. A small per-bar jitter (stable
 * per-bar phase derived from bar index, advanced by a ticker LaunchedEffect)
 * keeps things lively even when rmsLevel stalls — important because some
 * OEM ROMs (older Huawei EMUI is notorious) fire onRmsChanged at most a
 * couple of times per utterance, which would otherwise freeze the display.
 */
@Composable
private fun WaveformBars(
    rmsLevel: Float,
    modifier: Modifier = Modifier
) {
    // Stable per-bar phase so jitter doesn't reshuffle every recomposition.
    val phases = remember { FloatArray(7) { it * 0.7f } }
    var tick by remember { mutableStateOf(0) }
    LaunchedEffect(Unit) {
        while (true) {
            tick++
            // Advance phases; wrap into [0, 2π).
            for (i in phases.indices) {
                phases[i] = (phases[i] + 0.35f) % (2f * Math.PI.toFloat())
            }
            // 25fps is enough for a natural-looking waveform; faster wastes CPU.
            kotlinx.coroutines.delay(40L)
        }
    }
    val barColor = MaterialTheme.colorScheme.primary
    Canvas(modifier = modifier) {
        val barCount = 7
        val totalWidth = size.width
        val gap = 4.dp.toPx()
        val barWidth = (totalWidth - gap * (barCount - 1)) / barCount
        val cornerR = barWidth / 2f
        val baseLevel = rmsLevel.coerceIn(0.05f, 1f)
        // Symmetric decay: center bar tallest, edges shorter. Mimics the
        // "energy concentrated in the middle" look of WeChat / Telegram.
        val shape = floatArrayOf(0.45f, 0.65f, 0.85f, 1.0f, 0.85f, 0.65f, 0.45f)
        for (i in 0 until barCount) {
            // jitter in [0.85, 1.15] via sin
            val jitter = 1f + 0.15f * kotlin.math.sin(phases[i])
            val heightFraction = (baseLevel * shape[i] * jitter).coerceIn(0.08f, 1f)
            val barHeight = size.height * heightFraction
            val left = i * (barWidth + gap)
            val top = (size.height - barHeight) / 2f
            drawRoundRect(
                color = barColor,
                topLeft = androidx.compose.ui.geometry.Offset(left, top),
                size = androidx.compose.ui.geometry.Size(barWidth, barHeight),
                cornerRadius = androidx.compose.ui.geometry.CornerRadius(cornerR, cornerR)
            )
        }
    }
}

/**
 * v4.2.12 #3a-redesign: input-bar mode. Kimi-style toggle — clicking the mic
 * icon swaps the whole TextField out for a "按住 说话" hold-to-speak button,
 * and clicking the keyboard icon in voice mode swaps back.
 */
private enum class InputMode { TEXT, VOICE }

private val sttClient by lazy {
    OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()
}

/** Upload WAV to server Whisper, return transcribed text (or empty on failure). */
private suspend fun uploadForStt(wavFile: File, serverBaseUrl: String): String =
    withContext(Dispatchers.IO) {
        if (serverBaseUrl.isBlank()) return@withContext ""
        try {
            val reqBody = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("file", wavFile.name,
                    wavFile.asRequestBody("audio/wav".toMediaType()))
                .addFormDataPart("language", "zh")
                .build()
            val resp = sttClient.newCall(Request.Builder()
                .url("${serverBaseUrl}/api/stt/transcribe")
                .post(reqBody).build()).execute()
            val json = resp.body?.string() ?: ""
            org.json.JSONObject(json).optString("text", "")
        } catch (e: Exception) { "" }
    }

