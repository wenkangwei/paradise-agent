package com.example.aichat.ui.interact

import android.widget.Toast
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.example.aichat.ui.chat.rememberVoiceRecorder
import com.example.aichat.feature.live2d.Live2DLoadState
import com.example.aichat.feature.live2d.Live2DPhase
import com.example.aichat.feature.live2d.Live2DView
import com.example.aichat.ui.interact.components.HistoryCard
import com.example.aichat.ui.interact.components.ImmersiveControlBar
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/** Idle timeout before the control bar auto-hides. */
private const val INACTIVITY_TIMEOUT_MS = 5_000L

/**
 * Stage C entrypoint — Live2D interact tab.
 *
 * Layout (layered Box):
 *   Layer 0: PlaceholderAvatar (Live2D in Stage B)
 *   Layer 1: ImmersiveControlBar (bottom, alpha-animated)
 *   Layer 2: HistoryCard (only when expanded && !immersive)
 *
 * Tapping the avatar area (anywhere outside the control bar / history card)
 * immediately re-enters immersive mode. Tapping the control bar footprint
 * wakes the bar and resets the 5s idle timer.
 */
@Composable
fun InteractScreen(
    onOpenSettings: () -> Unit = {},
    viewModel: InteractViewModel = hiltViewModel(),
) {
    val ui by viewModel.ui.collectAsStateWithLifecycle()
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    // Push-to-talk recorder. rememberVoiceRecorder records WAV in a background
    // thread and calls back via onResult when finished. The callback arrives
    // on a non-main thread, so we hop onto the main coroutine scope before
    // touching the ViewModel.
    val recorder = rememberVoiceRecorder(context) { result ->
        scope.launch { viewModel.onRecordStop(result.wavFile) }
    }

    // Immersive UI state — local to the screen (not ViewModel) because it's
    // pure UI concern; survives config change via rememberSaveable.
    var immersive by rememberSaveable { mutableStateOf(true) }
    var historyExpanded by rememberSaveable { mutableStateOf(false) }
    var lastInteraction by rememberSaveable { mutableLongStateOf(0L) }

    // Unread badge: count AI replies that arrived while the history card was
    // collapsed. Reset to 0 whenever the user opens the card.
    val aiTurnCount = ui.history.count { it.role == ChatRole.AI && it.text.isNotBlank() }
    var lastSeenAiCount by rememberSaveable { mutableIntStateOf(0) }
    val unreadCount = (aiTurnCount - lastSeenAiCount).coerceAtLeast(0)
    LaunchedEffect(historyExpanded) {
        if (historyExpanded) lastSeenAiCount = aiTurnCount
    }

    // Auto-hide timer. Restart whenever lastInteraction changes.
    LaunchedEffect(lastInteraction) {
        if (lastInteraction > 0L) {
            delay(INACTIVITY_TIMEOUT_MS)
            immersive = true
            historyExpanded = false
        }
    }

    // Surface ViewModel errors as a toast.
    LaunchedEffect(ui.errorMessage) {
        ui.errorMessage?.let {
            Toast.makeText(context, it, Toast.LENGTH_SHORT).show()
        }
    }

    fun wake() {
        if (immersive) immersive = false
        lastInteraction = System.currentTimeMillis()
    }

    // Map the pipeline Phase to a coarse-grained Live2D motion state.
    // Speaking fires when we enter the Speaking phase; StoppedSpeaking fires
    // once when we leave it (back to Idle / Thinking). Tracked via a small
    // flag so we don't spam startSpeaking() on every recomposition.
    var wasSpeaking by rememberSaveable { mutableStateOf(false) }
    val live2dPhase = when {
        ui.phase == Phase.Speaking -> Live2DPhase.Speaking
        wasSpeaking && ui.phase != Phase.Speaking -> Live2DPhase.StoppedSpeaking
        else -> Live2DPhase.Idle
    }
    LaunchedEffect(ui.phase) {
        wasSpeaking = (ui.phase == Phase.Speaking)
    }

    Box(modifier = Modifier.fillMaxSize()) {
        // Layer 0: Live2D avatar (WebView). No pointerInput here — the
        // WebView is an Android View that consumes touch events internally,
        // which prevents Compose's detectTapGestures from ever firing.
        val loadState = Live2DView(
            phase = live2dPhase,
            modifier = Modifier.fillMaxSize(),
        )

        // Layer 0.5: Transparent full-screen tap-capture overlay. Sits ON TOP
        // of the WebView so the WebView never receives touches — taps land
        // here and toggle immersive ↔ awake.
        Box(
            modifier = Modifier
                .fillMaxSize()
                .pointerInput(Unit) {
                    detectTapGestures(onTap = {
                        if (immersive) {
                            wake()
                        } else {
                            immersive = true
                            historyExpanded = false
                        }
                    })
                },
        )

        // Loading / error overlay (Compose layer — above the WebGL canvas's
        // GPU compositing layer, which would paint over any HTML overlay).
        when (val ls = loadState) {
            is Live2DLoadState.Loading -> LoadingOverlay(ls.message)
            is Live2DLoadState.Error -> LoadingOverlay(
                message = ls.message,
                isError = true,
            )
            Live2DLoadState.Ready -> { /* model visible */ }
        }

        // Layer 1: Bottom control bar (immersive-animated).
        ImmersiveControlBar(
            phase = ui.phase,
            immersive = immersive,
            historyExpanded = historyExpanded,
            unreadCount = unreadCount,
            autoPlayTts = ui.autoPlayTts,
            onWake = ::wake,
            onToggleHistory = { historyExpanded = !historyExpanded },
            onPushToTalkStart = {
                if (!ui.phase.isBusy && recorder.isAvailable) {
                    recorder.start()
                    viewModel.onRecordStart()
                }
            },
            onPushToTalkEnd = {
                if (ui.phase == Phase.Recording) {
                    recorder.stop()
                }
            },
            onInterrupt = { viewModel.onInterrupt() },
            onToggleTts = { viewModel.toggleAutoPlayTts() },
            onOpenSettings = onOpenSettings,
            modifier = Modifier
                .align(Alignment.BottomCenter)
                .fillMaxWidth(),
        )

        // Layer 2: History card, slides up above the control bar.
        AnimatedVisibility(
            visible = historyExpanded && !immersive,
            enter = slideInVertically(initialOffsetY = { it / 2 }) + fadeIn(),
            exit = slideOutVertically(targetOffsetY = { it / 2 }) + fadeOut(),
            modifier = Modifier
                .align(Alignment.BottomCenter)
                .padding(bottom = 78.dp)
                .padding(horizontal = 12.dp),
        ) {
            HistoryCard(history = ui.history)
        }
    }
}

/**
 * Full-screen Compose loading overlay shown while the Live2D WebView
 * initializes pixi.js + loads the Hiyori model. Must be a Compose layer
 * (not HTML) because the WebGL canvas's GPU compositing layer paints over
 * any sibling DOM elements inside the WebView.
 */
@Composable
private fun LoadingOverlay(
    message: String,
    isError: Boolean = false,
) {
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color(0xE6121212)),
        contentAlignment = Alignment.Center,
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
        ) {
            if (isError) {
                Text(
                    text = "⚠",
                    color = Color(0xFFFF8A80),
                    style = MaterialTheme.typography.displayMedium,
                )
            } else {
                CircularProgressIndicator(
                    color = Color(0xFFFFCC00),
                    strokeWidth = 3.dp,
                )
            }
            Text(
                text = message,
                color = if (isError) Color(0xFFFF8A80) else Color(0xFFFFCC00),
                style = MaterialTheme.typography.bodyMedium,
                modifier = Modifier.padding(top = 16.dp),
            )
        }
    }
}
