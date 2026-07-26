package com.example.aichat.ui.chat

import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.ChatBubbleOutline
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.example.aichat.domain.model.Conversation
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale

@Composable
fun ConversationItem(
    conversation: Conversation,
    isSelected: Boolean,
    onClick: () -> Unit,
    onDelete: () -> Unit,
    /**
     * When `true`, this conversation currently has a STREAMING row in Room
     * (i.e. the `:streaming` process is actively producing an AI reply).
     * Renders a pulsing green dot on the item so the user can see at a
     * glance which sessions are still being replied to.
     */
    isStreaming: Boolean = false,
    modifier: Modifier = Modifier
) {
    val timeText = remember(conversation.updatedAt) {
        formatConversationListTime(conversation.updatedAt)
    }

    Card(
        onClick = onClick,
        modifier = modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = if (isSelected) {
                MaterialTheme.colorScheme.primaryContainer
            } else {
                MaterialTheme.colorScheme.surface
            }
        ),
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(12.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Icon(
                imageVector = Icons.Filled.ChatBubbleOutline,
                contentDescription = null,
                tint = if (isSelected) {
                    MaterialTheme.colorScheme.onPrimaryContainer
                } else {
                    MaterialTheme.colorScheme.onSurfaceVariant
                }
            )

            Column(
                modifier = Modifier
                    .weight(1f)
                    .padding(horizontal = 12.dp)
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        text = conversation.title.ifBlank { "New Conversation" },
                        style = MaterialTheme.typography.bodyMedium,
                        color = if (isSelected) {
                            MaterialTheme.colorScheme.onPrimaryContainer
                        } else {
                            MaterialTheme.colorScheme.onSurface
                        },
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f, fill = false)
                    )
                    if (isStreaming) {
                        StreamingDot()
                    }
                }

                if (conversation.lastMessage.isNotBlank()) {
                    Spacer(modifier = Modifier.height(2.dp))
                    Text(
                        text = conversation.lastMessage,
                        style = MaterialTheme.typography.bodySmall,
                        color = if (isSelected) {
                            MaterialTheme.colorScheme.onPrimaryContainer.copy(alpha = 0.7f)
                        } else {
                            MaterialTheme.colorScheme.onSurfaceVariant
                        },
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis
                    )
                }
            }

            Text(
                text = timeText,
                style = MaterialTheme.typography.labelSmall,
                color = if (isSelected) {
                    MaterialTheme.colorScheme.onPrimaryContainer.copy(alpha = 0.6f)
                } else {
                    MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.6f)
                }
            )

            IconButton(
                onClick = onDelete,
                modifier = Modifier.padding(start = 4.dp)
            ) {
                Icon(
                    imageVector = Icons.Filled.Delete,
                    contentDescription = "Delete conversation",
                    tint = if (isSelected) {
                        MaterialTheme.colorScheme.onPrimaryContainer.copy(alpha = 0.7f)
                    } else {
                        MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.7f)
                    }
                )
            }
        }
    }
}

/**
 * Pulsing green dot — indicates the conversation is currently being
 * streamed by the `:streaming` process. The breathing animation makes
 * it visible against any background color.
 */
@Composable
private fun StreamingDot() {
    val transition = rememberInfiniteTransition(label = "streaming-dot")
    val alpha by transition.animateFloat(
        initialValue = 0.35f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(800),
            repeatMode = RepeatMode.Reverse
        ),
        label = "streaming-dot-alpha"
    )
    Spacer(modifier = Modifier.size(6.dp))
    Box(
        modifier = Modifier
            .size(8.dp)
            .alpha(alpha)
            .background(Color(0xFF4CAF50), CircleShape)
    )
}

/**
 * Compact relative time for the conversation list (WeChat style):
 *   - Today:           "HH:mm"
 *   - Yesterday:       "昨天"
 *   - Day before y.:   "前天"
 *   - This year:       "MM-dd"
 *   - Older:           "yyyy-MM-dd"
 *
 * The list cell is narrow so we omit the clock for non-today rows; the
 * exact time is still visible inside the chat as a TimeDivider.
 */
private fun formatConversationListTime(timestamp: Long): String {
    val now = Calendar.getInstance()
    val msg = Calendar.getInstance().apply { timeInMillis = timestamp }

    val sameYear = now.get(Calendar.YEAR) == msg.get(Calendar.YEAR)
    val dayDiff = now.get(Calendar.DAY_OF_YEAR) - msg.get(Calendar.DAY_OF_YEAR)
    val isSameDay = sameYear && dayDiff == 0
    val isYesterday = sameYear && dayDiff == 1
    val isDayBeforeYesterday = sameYear && dayDiff == 2

    return when {
        isSameDay -> SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(timestamp))
        isYesterday -> "昨天"
        isDayBeforeYesterday -> "前天"
        sameYear -> SimpleDateFormat("MM-dd", Locale.getDefault()).format(Date(timestamp))
        else -> SimpleDateFormat("yyyy-MM-dd", Locale.getDefault()).format(Date(timestamp))
    }
}
