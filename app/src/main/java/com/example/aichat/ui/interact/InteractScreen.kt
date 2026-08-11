package com.example.aichat.ui.interact

import android.widget.Toast
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.example.aichat.ui.chat.rememberVoiceRecorder
import com.example.aichat.ui.interact.components.HistoryCard
import com.example.aichat.ui.interact.components.ImmersiveControlBar
import com.example.aichat.ui.interact.components.PlaceholderAvatar
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

    Box(modifier = Modifier.fillMaxSize()) {
        // Layer 0: Live2D placeholder. Tap on it hides controls (immersive).
        PlaceholderAvatar(
            phase = ui.phase,
            modifier = Modifier
                .fillMaxSize()
                .pointerInput(Unit) {
                    detectTapGestures(onTap = {
                        // Tap avatar → if currently awake, go immersive;
                        // if already immersive, do nothing (no wake from backdrop).
                        if (!immersive) {
                            immersive = true
                            historyExpanded = false
                        }
                    })
                },
        )

        // Layer 1: Bottom control bar (immersive-animated).
        ImmersiveControlBar(
            phase = ui.phase,
            immersive = immersive,
            historyExpanded = historyExpanded,
            latestPreview = ui.history.lastOrNull()?.text,
            onWake = ::wake,
            onToggleHistory = { historyExpanded = !historyExpanded },
            onPushToTalkStart = {
                // Block press while busy — let the 中断 button handle it.
                if (!ui.phase.isBusy && recorder.isAvailable) {
                    recorder.start()
                    viewModel.onRecordStart()
                }
            },
            onPushToTalkEnd = {
                if (ui.phase == Phase.Recording) {
                    recorder.stop()
                    // wav callback → viewModel.onRecordStop via rememberVoiceRecorder's onResult
                }
            },
            onInterrupt = { viewModel.onInterrupt() },
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
