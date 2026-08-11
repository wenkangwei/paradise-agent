package com.example.aichat.ui.interact.components

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.example.aichat.ui.interact.ChatRole
import com.example.aichat.ui.interact.ChatTurn

/**
 * Semi-transparent expandable history panel that floats above the bottom
 * control bar. Tap ✕ on the control bar to dismiss.
 *
 * The panel uses `LazyColumn` so long histories scroll efficiently; newest
 * entry auto-pins to the bottom via `LaunchedEffect` + `animateScrollToItem`.
 */
@Composable
fun HistoryCard(
    history: List<ChatTurn>,
    modifier: Modifier = Modifier,
) {
    val listState = rememberLazyListState()

    LaunchedEffect(history.size, history.lastOrNull()?.text) {
        if (history.isNotEmpty()) {
            // Jump (no animation) when a new turn is added; animate when
            // existing last turn's text grows (streaming).
            listState.animateScrollToItem(history.lastIndex)
        }
    }

    Surface(
        modifier = modifier
            .fillMaxWidth()
            .heightIn(min = 200.dp, max = 360.dp),
        shape = RoundedCornerShape(20.dp),
        color = Color.Black.copy(alpha = 0.45f),
    ) {
        if (history.isEmpty()) {
            Box(
                modifier = Modifier.fillMaxWidth().padding(32.dp),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    text = "暂无对话记录\n按住下方按钮开始对话",
                    color = Color.White.copy(alpha = 0.55f),
                    fontSize = 13.sp,
                    lineHeight = 20.sp,
                    textAlign = androidx.compose.ui.text.style.TextAlign.Center,
                )
            }
        } else {
            LazyColumn(
                state = listState,
                modifier = Modifier.fillMaxWidth(),
                contentPadding = PaddingValues(horizontal = 14.dp, vertical = 12.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                items(history) { turn ->
                    HistoryRow(turn)
                }
            }
        }
    }
}

@Composable
private fun HistoryRow(turn: ChatTurn) {
    Column(
        modifier = Modifier.fillMaxWidth(),
        horizontalAlignment = if (turn.role == ChatRole.USER) Alignment.End else Alignment.Start,
    ) {
        Text(
            text = turn.role.label,
            color = when (turn.role) {
                ChatRole.USER -> Color(0xFF81D4FA).copy(alpha = 0.85f)
                ChatRole.AI -> Color(0xFFA5D6A7).copy(alpha = 0.85f)
            },
            fontSize = 11.sp,
            fontWeight = FontWeight.Medium,
        )
        Spacer(Modifier.size(2.dp))
        Surface(
            shape = RoundedCornerShape(
                topStart = 14.dp,
                topEnd = 14.dp,
                bottomEnd = if (turn.role == ChatRole.USER) 4.dp else 14.dp,
                bottomStart = if (turn.role == ChatRole.USER) 14.dp else 4.dp,
            ),
            color = if (turn.role == ChatRole.USER)
                Color(0xFF1E88E5).copy(alpha = 0.35f)
            else
                Color.White.copy(alpha = 0.12f),
        ) {
            Text(
                text = turn.text.ifEmpty { "…" },
                color = Color.White.copy(alpha = 0.92f),
                fontSize = 14.sp,
                lineHeight = 20.sp,
                modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
            )
        }
    }
}
