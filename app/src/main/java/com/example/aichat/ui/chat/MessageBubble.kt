package com.example.aichat.ui.chat

import android.content.Intent
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ContentCopy
import androidx.compose.material.icons.filled.SelectAll
import androidx.compose.material.icons.filled.Share
import androidx.compose.material.icons.filled.ThumbDown
import androidx.compose.material.icons.filled.ThumbUp
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
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
 * Long-press on the text bubble opens a context menu (copy / select-all / share).
 * AI bubbles additionally render a like/dislike row at the bottom (Kimi style).
 * Streaming bubbles disable the long-press menu to avoid accidental triggers.
 *
 * Attachments and text are rendered in **separate bubbles** stacked vertically
 * (attachments on top, text below). Each attachment group gets its own visual
 * treatment via [MessageAttachmentList].
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun MessageBubble(
    message: ChatMessage,
    onReact: (String) -> Unit,
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
    val context = LocalContext.current
    val clipboard = LocalClipboardManager.current
    var showMenu by remember { mutableStateOf(false) }

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
                    modifier = Modifier
                        .widthIn(max = 320.dp)
                        .combinedClickable(
                            enabled = !message.isStreaming,
                            onClick = {},
                            onLongClick = { showMenu = true }
                        )
                ) {
                    Column(
                        modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp)
                    ) {
                        // Reasoning / thinking trace — only for AI + when present
                        if (!isUser && !message.reasoningContent.isNullOrBlank()) {
                            ReasoningSection(
                                reasoning = message.reasoningContent,
                                isStreaming = message.isStreaming && message.content.isBlank()
                            )
                            if (message.content.isNotBlank()) Spacer(Modifier.height(8.dp))
                        }

                        // Search results (RAG) — only for AI + when metadata carries them
                        if (!isUser) {
                            message.metadata?.searchResults?.takeIf { it.isNotEmpty() }?.let { results ->
                                SearchResultsSection(results = results)
                                if (message.content.isNotBlank()) Spacer(Modifier.height(8.dp))
                            }
                        }

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

                        // AI-only feedback row (like / dislike) — Kimi style.
                        // Shown only after streaming completes and content is non-empty.
                        if (!isUser && !message.isStreaming && message.content.isNotBlank()) {
                            Spacer(Modifier.height(6.dp))
                            ReactionRow(
                                reaction = message.reaction,
                                onReact = onReact
                            )
                        }
                    }
                }
            }
        }

        // Long-press context menu (anchored to the outer Box)
        DropdownMenu(
            expanded = showMenu,
            onDismissRequest = { showMenu = false }
        ) {
            DropdownMenuItem(
                text = { Text("复制") },
                leadingIcon = { Icon(Icons.Filled.ContentCopy, contentDescription = null) },
                onClick = {
                    clipboard.setText(AnnotatedString(message.content))
                    showMenu = false
                }
            )
            DropdownMenuItem(
                text = { Text("全选并复制") },
                leadingIcon = { Icon(Icons.Filled.SelectAll, contentDescription = null) },
                onClick = {
                    clipboard.setText(AnnotatedString(message.content))
                    showMenu = false
                }
            )
            DropdownMenuItem(
                text = { Text("分享") },
                leadingIcon = { Icon(Icons.Filled.Share, contentDescription = null) },
                onClick = {
                    val intent = Intent(Intent.ACTION_SEND).apply {
                        type = "text/plain"
                        putExtra(Intent.EXTRA_TEXT, message.content)
                    }
                    runCatching {
                        context.startActivity(Intent.createChooser(intent, null))
                    }
                    showMenu = false
                }
            )
        }
    }
}

@Composable
private fun ReactionRow(
    reaction: String?,
    onReact: (String) -> Unit
) {
    Row(
        horizontalArrangement = Arrangement.spacedBy(2.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        ReactionIcon(
            icon = Icons.Filled.ThumbUp,
            contentDescription = "点赞",
            isSelected = reaction == "like",
            onClick = { onReact("like") }
        )
        ReactionIcon(
            icon = Icons.Filled.ThumbDown,
            contentDescription = "点踩",
            isSelected = reaction == "dislike",
            onClick = { onReact("dislike") }
        )
    }
}

@Composable
private fun ReactionIcon(
    icon: ImageVector,
    contentDescription: String,
    isSelected: Boolean,
    onClick: () -> Unit
) {
    val tint = if (isSelected) MaterialTheme.colorScheme.primary
               else MaterialTheme.colorScheme.onSurfaceVariant
    IconButton(
        onClick = onClick,
        modifier = Modifier.size(28.dp)
    ) {
        Icon(
            imageVector = icon,
            contentDescription = contentDescription,
            tint = tint,
            modifier = Modifier.size(16.dp)
        )
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
