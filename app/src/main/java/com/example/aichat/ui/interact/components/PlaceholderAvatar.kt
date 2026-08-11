package com.example.aichat.ui.interact.components

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.example.aichat.ui.interact.Phase

/**
 * Stage B placeholder — a soft gradient backdrop with a "breathing" halo whose
 * color reflects the current conversation phase. Replaced by the real
 * Live2D Cubism GLSurfaceView once the SDK is integrated.
 *
 * Visual cues by phase:
 *   Idle        — calm cyan
 *   Recording   — warm red (rec dot vibe)
 *   Transcribing — soft amber
 *   Thinking    — indigo
 *   Speaking    — emerald (talk back)
 */
@Composable
fun PlaceholderAvatar(
    phase: Phase,
    modifier: Modifier = Modifier,
) {
    val haloColor = when (phase) {
        Phase.Idle -> Color(0xFF4DD0E1).copy(alpha = 0.55f)
        Phase.Recording -> Color(0xFFEF5350).copy(alpha = 0.7f)
        Phase.Transcribing -> Color(0xFFFFB74D).copy(alpha = 0.6f)
        Phase.Thinking -> Color(0xFF7986CB).copy(alpha = 0.6f)
        Phase.Speaking -> Color(0xFF66BB6A).copy(alpha = 0.65f)
    }

    val transition = rememberInfiniteTransition(label = "halo_breath")
    val breath by transition.animateFloat(
        initialValue = 0.85f,
        targetValue = 1.15f,
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = if (phase == Phase.Speaking) 600 else 2200, easing = LinearEasing),
            repeatMode = RepeatMode.Reverse,
        ),
        label = "scale",
    )

    Box(
        modifier = modifier
            .fillMaxSize()
            .background(
                Brush.verticalGradient(
                    colors = listOf(
                        Color(0xFF1A1A2E),
                        Color(0xFF16213E),
                        Color(0xFF0F3460),
                    )
                )
            ),
        contentAlignment = Alignment.Center,
    ) {
        Canvas(modifier = Modifier.fillMaxSize()) {
            val canvasWidth = size.width
            val canvasHeight = size.height
            val center = Offset(canvasWidth / 2f, canvasHeight / 2f)
            val baseRadius = minOf(canvasWidth, canvasHeight) * 0.18f
            val radius = baseRadius * breath

            // Outer halo
            drawCircle(
                color = haloColor,
                radius = radius * 1.6f,
                center = center,
                alpha = 0.18f,
            )
            drawCircle(
                color = haloColor,
                radius = radius * 1.2f,
                center = center,
                alpha = 0.30f,
            )
            // Inner orb
            drawCircle(
                color = haloColor.copy(alpha = 0.9f),
                radius = radius,
                center = center,
            )
        }

        Text(
            text = "Live2D 占位",
            color = Color.White.copy(alpha = 0.35f),
            fontSize = 12.sp,
            fontWeight = FontWeight.Light,
            modifier = Modifier.align(Alignment.BottomCenter),
        )
    }
}
