package com.example.aichat.ui.interact.components

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Forum
import androidx.compose.material.icons.filled.MoreHoriz
import androidx.compose.material.icons.filled.StopCircle
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.example.aichat.ui.interact.Phase

/**
 * 沉浸式底部控制栏 — 一行四按钮 + 自动隐藏 + 唤醒 hot zone.
 *
 * Layout: [💬/✕] [🎤 按住说话] [⏹中断] [⚙]
 *
 * Behavior:
 *   - When `immersive == true`: entire bar alpha = 0, buttons disabled.
 *     Tapping anywhere on the bar's footprint triggers `onWake`.
 *   - When `immersive == false`: bar fully visible; buttons interactive;
 *     every tap/click inside also calls `onWake` to reset the inactivity timer.
 *   - 按住说话 uses press-and-hold via detectTapGestures.onPress:
 *       down → onPushToTalkStart, up (or cancel) → onPushToTalkEnd.
 *   - 中断 enabled only when phase != Idle (otherwise button is grayed).
 *
 * The bubble button (💬/✕) shows a small preview chip of the latest
 * conversation turn when collapsed (or just 💬 when no history).
 */
@Composable
fun ImmersiveControlBar(
    phase: Phase,
    immersive: Boolean,
    historyExpanded: Boolean,
    latestPreview: String?,
    onWake: () -> Unit,
    onToggleHistory: () -> Unit,
    onPushToTalkStart: () -> Unit,
    onPushToTalkEnd: () -> Unit,
    onInterrupt: () -> Unit,
    onOpenSettings: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val alpha by animateFloatAsState(
        targetValue = if (immersive) 0f else 1f,
        label = "control_bar_alpha",
    )

    Box(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 8.dp),
    ) {
        // Invisible hot zone that captures taps even when buttons are hidden.
        // Always present (in addition to the visible bar) so taps on the
        // bar's footprint wake the UI regardless of immersive state.
        Box(
            modifier = Modifier
                .matchParentSize()
                .pointerInput(immersive) {
                    detectTapGestures(
                        onTap = { onWake() },
                    )
                },
        )

        // Visible control row. Hidden state still occupies the layout slot
        // (so the hot zone has size), but interaction is gated by `enabled`.
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .alpha(alpha),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            // 💬 / ✕
            BubbleToggleButton(
                expanded = historyExpanded,
                latestPreview = latestPreview,
                enabled = !immersive,
                onClick = { onWake(); onToggleHistory() },
            )

            // 🎤 按住说话 — weight() must be called in the RowScope (here),
            // then handed down to PushToTalkButton's Surface as plain Modifier.
            PushToTalkButton(
                phase = phase,
                enabled = !immersive,
                onPressStart = { onWake(); onPushToTalkStart() },
                onPressEnd = { onPushToTalkEnd() },
                modifier = Modifier.weight(1f),
            )

            // ⏹ 中断
            InterruptButton(
                enabled = !immersive && phase.isBusy,
                onClick = { onWake(); onInterrupt() },
            )

            // ⚙
            SettingsButton(
                enabled = !immersive,
                onClick = { onWake(); onOpenSettings() },
            )
        }
    }
}

@Composable
private fun BarSlot(
    enabled: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    content: @Composable () -> Unit,
) {
    val haptics = LocalHapticFeedback.current
    Surface(
        modifier = modifier.pointerInput(enabled) {
            if (enabled) detectTapGestures(
                onTap = {
                    haptics.performHapticFeedback(androidx.compose.ui.hapticfeedback.HapticFeedbackType.LongPress)
                    onClick()
                },
            )
        },
        shape = RoundedCornerShape(28.dp),
        color = Color.Black.copy(alpha = 0.45f),
    ) {
        Box(
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
            contentAlignment = Alignment.Center,
        ) { content() }
    }
}

@Composable
private fun BubbleToggleButton(
    expanded: Boolean,
    latestPreview: String?,
    enabled: Boolean,
    onClick: () -> Unit,
) {
    BarSlot(enabled = enabled, onClick = onClick) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(
                imageVector = if (expanded) Icons.Filled.Close else Icons.Filled.Forum,
                contentDescription = if (expanded) "收起对话历史" else "展开对话历史",
                tint = Color.White,
                modifier = Modifier.size(22.dp),
            )
            if (!expanded && !latestPreview.isNullOrBlank()) {
                Spacer(Modifier.width(6.dp))
                Text(
                    text = latestPreview.take(10),
                    color = Color.White.copy(alpha = 0.75f),
                    fontSize = 11.sp,
                    maxLines = 1,
                    fontWeight = FontWeight.Medium,
                )
            }
        }
    }
}

@Composable
private fun PushToTalkButton(
    phase: Phase,
    enabled: Boolean,
    onPressStart: () -> Unit,
    onPressEnd: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val isRecording = (phase == Phase.Recording)
    val haptics = LocalHapticFeedback.current

    Surface(
        modifier = modifier
            .pointerInput(enabled) {
                if (!enabled) return@pointerInput
                detectTapGestures(
                    onPress = {
                        haptics.performHapticFeedback(androidx.compose.ui.hapticfeedback.HapticFeedbackType.LongPress)
                        onPressStart()
                        val succeeded = tryAwaitRelease()
                        onPressEnd()
                        if (!succeeded) onPressEnd()  // ensure paired
                    },
                )
            },
        shape = RoundedCornerShape(28.dp),
        color = when {
            isRecording -> Color(0xFFEF5350).copy(alpha = 0.7f)
            phase.isBusy -> Color(0xFF37474F).copy(alpha = 0.55f)
            else -> Color(0xFF1E88E5).copy(alpha = 0.45f)
        },
    ) {
        Box(
            modifier = Modifier
                .padding(horizontal = 18.dp, vertical = 12.dp),
            contentAlignment = Alignment.Center,
        ) {
            Text(
                text = when {
                    isRecording -> "松开 发送"
                    phase == Phase.Transcribing -> "识别中…"
                    phase == Phase.Thinking -> "思考中…"
                    phase == Phase.Speaking -> "回复中…"
                    else -> "🎤 按住 说话"
                },
                color = Color.White,
                fontSize = 15.sp,
                fontWeight = FontWeight.Medium,
            )
        }
    }
}

@Composable
private fun InterruptButton(
    enabled: Boolean,
    onClick: () -> Unit,
) {
    BarSlot(enabled = enabled, onClick = onClick) {
        Icon(
            imageVector = Icons.Filled.StopCircle,
            contentDescription = "中断",
            tint = if (enabled) Color(0xFFFF8A80) else Color.White.copy(alpha = 0.35f),
            modifier = Modifier.size(24.dp),
        )
    }
}

@Composable
private fun SettingsButton(
    enabled: Boolean,
    onClick: () -> Unit,
) {
    BarSlot(enabled = enabled, onClick = onClick) {
        Icon(
            imageVector = Icons.Filled.MoreHoriz,
            contentDescription = "更多",
            tint = Color.White.copy(alpha = if (enabled) 0.85f else 0.35f),
            modifier = Modifier.size(22.dp),
        )
    }
}
