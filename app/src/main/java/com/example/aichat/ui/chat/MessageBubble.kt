package com.example.aichat.ui.chat

import android.content.Intent
import androidx.compose.animation.AnimatedContent
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
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Share
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material.icons.filled.ThumbDown
import androidx.compose.material.icons.filled.ThumbUp
import androidx.compose.material.icons.filled.VolumeUp
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import kotlinx.coroutines.delay
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
import com.example.aichat.domain.model.MessageStatus
import com.example.aichat.ui.chat.model.ChatMessage
import com.example.aichat.ui.chat.model.Role
import com.example.aichat.ui.chat.toolcard.CardSegment
import com.example.aichat.ui.chat.toolcard.ToolCard
import com.example.aichat.ui.chat.toolcard.ToolCardRecognizer
import com.example.aichat.ui.chat.toolcard.ToolType
import com.example.aichat.ui.chat.toolcard.deriveToolTitle
import com.example.aichat.ui.theme.chatColors
import com.example.aichat.util.HonorOemHelper

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
/**
 * Tool-card callbacks. Each callback receives the segment's source content
 * and type so the caller (ChatScreen) can route to the full-screen sheet,
 * the favorite-tool dialog, or the share intent without re-parsing.
 */
typealias ToolCardAction = (content: String, type: ToolType) -> Unit

@OptIn(ExperimentalFoundationApi::class)
@Composable
fun MessageBubble(
    message: ChatMessage,
    onReact: (String) -> Unit,
    onRetry: () -> Unit = {},
    onOpenTool: ToolCardAction = { _, _ -> },
    onCollectTool: ToolCardAction = { _, _ -> },
    onShareTool: ToolCardAction = { _, _ -> },
    /**
     * v4.2.12 #5: fired when the user taps the share icon on this bubble
     * (or picks 分享 from the long-press menu). The screen opens a
     * multi-select sheet pre-loaded with this message; the user can add
     * more messages and then share them all as a single text block.
     * Replaces the old behavior of immediately firing ACTION_SEND with
     * just this one message.
     */
    onShareMessage: () -> Unit = {},
    onSpeak: () -> Unit = {},
    isSpeaking: Boolean = false,
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

    // v4.2.1: AI replies are flat (no bubble background / no tonal elevation /
    // no max-width) so consecutive AI messages read as one coherent stream.
    // User bubbles keep their tinted surface + 320dp max width.
    val aiFlatColor = Color.Transparent
    val aiTonal = 0.dp

    Box(
        modifier = modifier.fillMaxWidth(),
        contentAlignment = alignment
    ) {
        Column(
            horizontalAlignment = if (isUser) Alignment.End else Alignment.Start,
            verticalArrangement = Arrangement.spacedBy(4.dp),
            modifier = Modifier
                .padding(horizontal = 4.dp, vertical = 2.dp)
                .fillMaxWidth()
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

            // Tool-card segmentation: only for non-streaming AI messages whose
            // body actually contains a "tool" (full HTML page / long markdown
            // document). Everything else — user text, streaming partials, plain
            // markdown replies — falls through to the legacy single-bubble path.
            val segments = remember(message.content, message.isStreaming, isUser) {
                if (isUser || message.isStreaming || message.content.isBlank()) {
                    emptyList()
                } else {
                    ToolCardRecognizer.split(message.content)
                }
            }
            val hasToolCards = segments.any { it is CardSegment.Tool }

            if (!hasToolCards) {
                // 2) Text bubble (or streaming cursor placeholder)
                // v4.2.8: also render when reasoningContent is non-blank, so
                // thinking-model replies whose content body was lost (lock-screen
                // socket abort → SSE retry silent-end) still show the reasoning
                // trace instead of vanishing entirely. Belt-and-suspenders with
                // StreamAiReplyUseCase now writing a placeholder.
                if (message.content.isNotBlank() ||
                    !message.reasoningContent.isNullOrBlank() ||
                    (message.isStreaming && message.attachments.isEmpty())) {
                    Surface(
                        shape = if (isUser) bubbleShape else RoundedCornerShape(0.dp),
                        color = if (isUser) chatColors.userBubbleColor else aiFlatColor,
                        tonalElevation = if (isUser) 2.dp else aiTonal,
                        modifier = Modifier
                            .widthIn(max = if (isUser) 320.dp else 9999.dp)
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

                            // Search results button — opens full-screen card list
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
                                } else if (!message.isStreaming && looksLikeHtmlPage(message.content)) {
                                    // Complete HTML page - render as an embedded WebView card
                                    HtmlCard(
                                        html = message.content,
                                        onOpenFullScreen = { onOpenTool(message.content, ToolType.HTML) }
                                    )
                                } else {
                                    // v4.2.12: SelectionContainer restored (now in
                                    // MarkdownText itself) since Compose BOM 1.7.x
                                    // makes the native selection popup coexist
                                    // cleanly with our long-press DropdownMenu.
                                    MarkdownText(
                                        text = message.content,
                                        style = MaterialTheme.typography.bodyMedium.copy(
                                            color = chatColors.onAiBubbleColor
                                        )
                                    )
                                }
                            }

                            // v4.2.12: 长按文字本身 → native selection popup (Copy/Select All)
                            // 由 MarkdownText 内部的 SelectionContainer 提供。
                            // 长按气泡空白处 → 我们的 DropdownMenu (点赞/点踩/复制全文/分享)。
                            // 两个 popup 共存，各管各的。
                            //
                            // 用户期望的「复制所选」由 native popup 的 Copy 完成；
                            // 想读 SelectionContainer 当前 selection 的代码很复杂
                            // (LocalSelectionRegistrar + 手动 TextRange 映射)，
                            // 收益又有限（native popup 已经做了），所以这里不重复。
                            if (message.isStreaming && message.content.isBlank() && message.reasoningContent.isNullOrBlank()) {
                                // No content yet AND no reasoning — animated thinking indicator.
                                StreamingPlaceholder(color = chatColors.onAiBubbleColor)
                            } else if (message.isStreaming && message.content.isNotBlank()) {
                                Spacer(Modifier.height(2.dp))
                                StreamingCursor(color = chatColors.onAiBubbleColor)
                            }

                            if (!isUser && !message.isStreaming && message.content.isNotBlank()) {
                                Spacer(Modifier.height(6.dp))
                                ReactionRow(
                                    reaction = message.reaction,
                                    onReact = onReact,
                                    onCopyAll = {
                                        clipboard.setText(AnnotatedString(message.content))
                                    },
                                    onShare = onShareMessage,
                                    onRetry = onRetry,
                                    onSpeak = onSpeak,
                                    isSpeaking = isSpeaking
                                )
                            }

                            // v4.2.12: ReactionRow already carries a Refresh
                            // icon for every completed AI bubble, so the
                            // standalone RetryButton under FAILED is now
                            // redundant — kept only for INTERRUPTED states
                            // (where the more prominent affordance helps
                            // surface that the message is in an error state,
                            // not just normal completion).
                            if (!isUser &&
                                message.status != MessageStatus.COMPLETE &&
                                !message.isStreaming
                            ) {
                                Spacer(Modifier.height(8.dp))
                                RetryButton(onClick = onRetry)
                            }

                            // v4.2.11: Honor PGManager-killed reply — offer a
                            // one-tap shortcut to Honor's App Launch Management
                            // settings. Only shown when the message metadata
                            // carries errorCategory = "honor_oem_kill" (set by
                            // StreamAiReplyUseCase when stream silent-ends on
                            // a Honor device with partial content/reasoning).
                            if (!isUser &&
                                message.metadata?.errorCategory == "honor_oem_kill"
                            ) {
                                Spacer(Modifier.height(8.dp))
                                HonorSettingsButton(onClick = {
                                    HonorOemHelper.openAppLaunchManagement(context)
                                })
                            }
                        }
                    }
                }
            } else {
                // Segmented rendering: text stays in bubbles (max-width 320dp),
                // tool cards break out to the full content width so tables /
                // SVGs / wide layouts aren't crushed.
                //
                // Reasoning + search results are pulled out to their own
                // leading bubble so they read as preamble, not as the first
                // segment.
                if (!isUser && !message.reasoningContent.isNullOrBlank()) {
                    Surface(
                        shape = RoundedCornerShape(0.dp),
                        color = aiFlatColor,
                        tonalElevation = aiTonal,
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Column(modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp)) {
                            ReasoningSection(
                                reasoning = message.reasoningContent,
                                isStreaming = false
                            )
                        }
                    }
                }
                if (!isUser) {
                    message.metadata?.searchResults?.takeIf { it.isNotEmpty() }?.let { results ->
                        Surface(
                            shape = RoundedCornerShape(0.dp),
                            color = aiFlatColor,
                            tonalElevation = aiTonal,
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Column(modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp)) {
                                SearchResultsSection(results = results)
                            }
                        }
                    }
                }

                segments.forEachIndexed { idx, segment ->
                    when (segment) {
                        is CardSegment.Text -> {
                            Surface(
                                shape = RoundedCornerShape(0.dp),
                                color = aiFlatColor,
                                tonalElevation = aiTonal,
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .combinedClickable(
                                        enabled = true,
                                        onClick = {},
                                        onLongClick = { showMenu = true }
                                    )
                            ) {
                                Column(
                                    modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp)
                                ) {
                                    MarkdownText(
                                        text = segment.markdown,
                                        style = MaterialTheme.typography.bodyMedium.copy(
                                            color = chatColors.onAiBubbleColor
                                        )
                                    )
                                    // Streaming cursor only on the last text segment
                                    // (mirrors the single-bubble behaviour above).
                                    if (idx == segments.lastIndex &&
                                        message.isStreaming && message.content.isNotBlank()
                                    ) {
                                        Spacer(Modifier.height(2.dp))
                                        StreamingCursor(color = chatColors.onAiBubbleColor)
                                    }
                                    // v4.2.12 #5: ReactionRow + RetryButton +
                                    // HonorSettingsButton moved OUT of this loop
                                    // (rendered once after the loop). Previously
                                    // they were attached to the last segment's
                                    // Column, which meant they silently
                                    // disappeared whenever the message ended
                                    // with a Tool card (e.g. an HTML page).
                                    // Surfacing them as their own trailing
                                    // block makes the actions available on
                                    // EVERY completed AI bubble regardless
                                    // of how the body was segmented.
                                }
                            }
                        }
                        is CardSegment.Tool -> {
                            val title = remember(segment.content, segment.type) {
                                deriveToolTitle(segment.content, segment.type)
                            }
                            ToolCard(
                                title = title,
                                content = segment.content,
                                type = segment.type,
                                onOpen = { onOpenTool(segment.content, segment.type) },
                                onCollect = { onCollectTool(segment.content, segment.type) },
                                onShare = { onShareTool(segment.content, segment.type) }
                            )
                        }
                    }
                }

                // v4.2.12 #5: action row always renders after the segments
                // loop in its own transparent Surface. This guarantees the
                // retry/share/like buttons show on EVERY completed AI bubble
                // — including ones whose body ends with a Tool card (the
                // bug we're fixing here: previously the action row only
                // rendered inside the last segment, which silently failed
                // when the last segment was a Tool).
                if (!isUser && !message.isStreaming && message.content.isNotBlank()) {
                    Surface(
                        shape = RoundedCornerShape(0.dp),
                        color = aiFlatColor,
                        tonalElevation = aiTonal,
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Column(modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp)) {
                            Spacer(Modifier.height(6.dp))
                            ReactionRow(
                                reaction = message.reaction,
                                onReact = onReact,
                                onCopyAll = {
                                    clipboard.setText(AnnotatedString(message.content))
                                },
                                onShare = onShareMessage,
                                onRetry = onRetry,
                                onSpeak = onSpeak,
                                isSpeaking = isSpeaking
                            )
                            if (message.status != MessageStatus.COMPLETE) {
                                Spacer(Modifier.height(8.dp))
                                RetryButton(onClick = onRetry)
                            }
                            if (message.metadata?.errorCategory == "honor_oem_kill") {
                                Spacer(Modifier.height(8.dp))
                                HonorSettingsButton(onClick = {
                                    HonorOemHelper.openAppLaunchManagement(context)
                                })
                            }
                        }
                    }
                }

                // Streaming placeholder when the model hasn't started emitting
                // content yet (e.g. thinking model cold start).
                if (message.isStreaming && message.content.isBlank() && message.reasoningContent.isNullOrBlank()) {
                    Surface(
                        shape = RoundedCornerShape(0.dp),
                        color = aiFlatColor,
                        tonalElevation = aiTonal,
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Box(modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp)) {
                            StreamingPlaceholder(color = chatColors.onAiBubbleColor)
                        }
                    }
                }
            }
        }

        // Long-press context menu — v4.2.4 expanded to a full action set.
        //
        // The user's request was to have 点赞 / 点踩 / 复制 / 分享 available
        // in the popup that appears after selecting text. The native
        // SelectionContainer popup in Compose 1.6.x is provided by the
        // platform and only shows Copy / Select All — it isn't customisable
        // without upgrading to Compose 1.7+ (textContextMenuItem API).
        //
        // As a working substitute, this long-press menu carries the full
        // action set. Combined with the always-visible reaction row under
        // AI bubbles, the user has one-tap access to every action without
        // depending on the system selection popup.
        DropdownMenu(
            expanded = showMenu,
            onDismissRequest = { showMenu = false }
        ) {
            if (!isUser) {
                DropdownMenuItem(
                    text = {
                        Text(if (message.reaction == "like") "取消点赞" else "点赞")
                    },
                    leadingIcon = { Icon(Icons.Filled.ThumbUp, contentDescription = null) },
                    onClick = {
                        // setMessageReaction toggles: if already liked → null,
                        // otherwise → "like". A single call flips the state.
                        onReact("like")
                        showMenu = false
                    }
                )
                DropdownMenuItem(
                    text = {
                        Text(if (message.reaction == "dislike") "取消点踩" else "点踩")
                    },
                    leadingIcon = { Icon(Icons.Filled.ThumbDown, contentDescription = null) },
                    onClick = {
                        onReact("dislike")
                        showMenu = false
                    }
                )
            }
            DropdownMenuItem(
                text = { Text("复制全文") },
                leadingIcon = { Icon(Icons.Filled.ContentCopy, contentDescription = null) },
                onClick = {
                    clipboard.setText(AnnotatedString(message.content))
                    showMenu = false
                }
            )
            DropdownMenuItem(
                text = { Text("分享") },
                leadingIcon = { Icon(Icons.Filled.Share, contentDescription = null) },
                onClick = {
                    showMenu = false
                    onShareMessage()
                }
            )
        }
    }
}

@Composable
private fun ReactionRow(
    reaction: String?,
    onReact: (String) -> Unit,
    onCopyAll: () -> Unit = {},
    onShare: () -> Unit = {},
    onRetry: () -> Unit = {},
    onSpeak: () -> Unit = {},
    isSpeaking: Boolean = false
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
        ReactionIcon(
            icon = Icons.Filled.ContentCopy,
            contentDescription = "复制全文",
            isSelected = false,
            onClick = onCopyAll
        )
        ReactionIcon(
            icon = Icons.Filled.Share,
            contentDescription = "分享",
            isSelected = false,
            onClick = onShare
        )
        // v4.2.12 #2: every completed AI bubble exposes retry. For
        // FAILED/INTERRUPTED messages ChatViewModel reuses the same
        // aiMessageId (in-place replace); for COMPLETE ones it mints a
        // fresh UUID so the prior reply is preserved as a new row.
        ReactionIcon(
            icon = Icons.Filled.Refresh,
            contentDescription = "重试",
            isSelected = false,
            onClick = onRetry
        )
        // v4.2.12 #3b: TTS playback. Icon swaps to Stop while this exact
        // message is playing; tapping a different message's icon stops the
        // current one first (handled by TtsController).
        ReactionIcon(
            icon = if (isSpeaking) Icons.Filled.Stop else Icons.Filled.VolumeUp,
            contentDescription = if (isSpeaking) "停止朗读" else "朗读",
            isSelected = isSpeaking,
            onClick = onSpeak
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

/**
 * Animated thinking indicator shown in the AI bubble before the first token
 * arrives — replaces the static blinking cursor with a small rotating
 * progress ring + cycling status text ("拼命思考中..." → ...).
 */
@Composable
private fun StreamingPlaceholder(color: Color) {
    val phrases = listOf("拼命思考中...", "正在组织语言...", "马上就好...", "再等一下...")
    var index by remember { mutableStateOf(0) }

    LaunchedEffect(Unit) {
        while (true) {
            delay(2_200L)
            index = (index + 1) % phrases.size
        }
    }

    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(10.dp)
    ) {
        CircularProgressIndicator(
            modifier = Modifier.size(18.dp),
            strokeWidth = 2.dp,
            color = color.copy(alpha = 0.6f)
        )
        AnimatedContent(
            targetState = phrases[index],
            label = "thinking-status"
        ) { phrase ->
            Text(
                text = phrase,
                color = color.copy(alpha = 0.7f),
                style = MaterialTheme.typography.bodyMedium
            )
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

/**
 * Retry affordance shown under a FAILED AI bubble. Uses FilledTonalButton so
 * the action is obvious but doesn't compete with the primary send button
 * (which is in the input bar). Compact size so it doesn't dominate the bubble.
 */
@Composable
private fun RetryButton(
    onClick: () -> Unit,
    modifier: Modifier = Modifier
) {
    FilledTonalButton(
        onClick = onClick,
        modifier = modifier,
        contentPadding = androidx.compose.foundation.layout.PaddingValues(
            horizontal = 12.dp,
            vertical = 0.dp
        )
    ) {
        Icon(
            imageVector = Icons.Filled.Refresh,
            contentDescription = null,
            modifier = Modifier.size(14.dp)
        )
        Spacer(Modifier.size(6.dp))
        Text(
            text = "重试",
            style = MaterialTheme.typography.labelMedium
        )
    }
}

/**
 * v4.2.11: Action button shown on replies killed by Honor PGManager.
 * Launches Honor's App Launch Management settings so the user can
 * whitelist the app — the only effective fix for Honor's kernel-level
 * socket destruction on lock-screen.
 */
@Composable
private fun HonorSettingsButton(
    onClick: () -> Unit,
    modifier: Modifier = Modifier
) {
    FilledTonalButton(
        onClick = onClick,
        modifier = modifier,
        contentPadding = androidx.compose.foundation.layout.PaddingValues(
            horizontal = 12.dp,
            vertical = 0.dp
        )
    ) {
        Icon(
            imageVector = Icons.Filled.Refresh,
            contentDescription = null,
            modifier = Modifier.size(14.dp)
        )
        Spacer(Modifier.size(6.dp))
        Text(
            text = "打开 Honor 设置",
            style = MaterialTheme.typography.labelMedium
        )
    }
}
