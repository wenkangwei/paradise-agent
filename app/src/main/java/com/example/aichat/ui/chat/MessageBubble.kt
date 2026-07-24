package com.example.aichat.ui.chat

import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import com.example.aichat.ui.chat.model.ChatMessage
import com.example.aichat.ui.chat.model.Role
import com.example.aichat.ui.theme.chatColors

/**
 * Renders a single chat message. User messages are right-aligned; assistant
 * messages are left-aligned.
 *
 * Attachments and text are rendered in **separate bubbles** stacked vertically
 * (attachments on top, text below) — previously they were crammed into one
 * bubble which made multi-file messages look like a wall of mixed content.
 * Each attachment group (images vs files) gets its own visual treatment via
 * [MessageAttachmentList].
 */
@Composable
fun MessageBubble(
    message: ChatMessage,
    modifier: Modifier = Modifier
) {
    val isUser = message.role == Role.USER
    val alignment: Alignment = if (isUser) Alignment.TopEnd else Alignment.TopStart
    val bubbleShape = if (isUser) {
        RoundedCornerShape(topStart = 16.dp, topEnd = 16.dp, bottomStart = 16.dp, bottomEnd = 4.dp)
    } else {
        RoundedCornerShape(topStart = 16.dp, topEnd = 16.dp, bottomStart = 4.dp, bottomEnd = 16.dp)
    }

    val chatColors = MaterialTheme.chatColors

    Box(
        modifier = modifier.fillMaxWidth(),
        contentAlignment = alignment
    ) {
        Column(
            horizontalAlignment = if (isUser) Alignment.End else Alignment.Start,
            verticalArrangement = Arrangement.spacedBy(4.dp),
            modifier = Modifier
                .padding(horizontal = 4.dp, vertical = 2.dp)
                .widthIn(max = 320.dp)
        ) {
            // 1) Attachment bubble (its own surface, distinct from text)
            if (message.attachments.isNotEmpty()) {
                Surface(
                    shape = RoundedCornerShape(16.dp),
                    color = if (isUser) chatColors.userBubbleColor else chatColors.aiBubbleColor,
                    tonalElevation = if (isUser) 2.dp else 1.dp,
                    modifier = Modifier.widthIn(max = 320.dp)
                ) {
                    MessageAttachmentList(
                        attachments = message.attachments,
                        modifier = Modifier.padding(8.dp)
                    )
                }
            }

            // 2) Text bubble (or streaming cursor placeholder)
            if (message.content.isNotBlank() || (message.isStreaming && message.attachments.isEmpty())) {
                Surface(
                    shape = bubbleShape,
                    color = if (isUser) chatColors.userBubbleColor else chatColors.aiBubbleColor,
                    tonalElevation = if (isUser) 2.dp else 1.dp,
                    modifier = Modifier.widthIn(max = 320.dp)
                ) {
                    Column(
                        modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp)
                    ) {
                        if (message.content.isNotBlank()) {
                            if (isUser) {
                                Text(
                                    text = message.content,
                                    color = chatColors.onUserBubbleColor,
                                    style = MaterialTheme.typography.bodyMedium
                                )
                            } else {
                                MarkdownText(
                                    text = message.content,
                                    style = MaterialTheme.typography.bodyMedium.copy(
                                        color = chatColors.onAiBubbleColor
                                    )
                                )
                            }
                        }

                        if (message.isStreaming) {
                            if (message.content.isBlank()) {
                                StreamingCursor(color = chatColors.streamingCursor)
                            } else {
                                Spacer(Modifier.height(2.dp))
                                StreamingCursor(color = chatColors.onAiBubbleColor)
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun StreamingCursor(
    color: Color,
    modifier: Modifier = Modifier
) {
    val infiniteTransition = rememberInfiniteTransition(label = "cursor")
    val alpha by infiniteTransition.animateFloat(
        initialValue = 1f,
        targetValue = 0.2f,
        animationSpec = infiniteRepeatable(
            animation = tween(500),
            repeatMode = RepeatMode.Reverse
        ),
        label = "cursorAlpha"
    )

    Text(
        text = buildAnnotatedString {
            withStyle(SpanStyle(fontWeight = FontWeight.Bold)) {
                append("\u258D")
            }
        },
        color = color,
        modifier = modifier.alpha(alpha),
        style = MaterialTheme.typography.bodyMedium
    )
}
