package com.example.aichat.ui.chat.toolcard

import androidx.compose.foundation.background
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Code
import androidx.compose.material.icons.filled.Fullscreen
import androidx.compose.material.icons.filled.Share
import androidx.compose.material.icons.filled.Star
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.example.aichat.ui.theme.chatColors

/**
 * A break-out card shown for tool segments (full HTML pages or long Markdown
 * documents) inside an AI reply. Unlike a regular chat bubble, this card spans
 * the full content width and offers three affordances:
 *
 *   ⤢ open in full-screen sheet
 *   ⭐ save to the drawer's "收藏工具" section
 *   ↗ share as a standalone file (HTML or .md)
 *
 * The card itself shows only a preview — first [PREVIEW_LINES] lines of the
 * source — so the user can scan many cards without scrolling forever. Full
 * content is one tap away via the ⤢ button (or tapping the body).
 *
 * @param title header label; usually derived via [deriveToolTitle].
 * @param onOpen invoked when the user taps the card body or the ⤢ button.
 *   The caller is responsible for showing [ToolCardFullScreen] in response.
 */
@Composable
fun ToolCard(
    title: String,
    content: String,
    type: ToolType,
    onOpen: () -> Unit,
    onCollect: () -> Unit,
    onShare: () -> Unit,
    modifier: Modifier = Modifier
) {
    val chatColors = MaterialTheme.chatColors
    val totalLines = remember(content) { content.lineSequence().count { it.isNotBlank() } }
    val previewLines = remember(content) {
        content.lineSequence().filter { it.isNotBlank() }.take(PREVIEW_LINES).toList()
    }

    Surface(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(12.dp),
        color = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.4f),
        tonalElevation = 2.dp,
        border = BorderStroke(
            1.dp,
            MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.5f)
        )
    ) {
        Column(modifier = Modifier.padding(12.dp)) {
            // Header: type icon + title + action buttons
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Icon(
                    imageVector = Icons.Filled.Code,
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.size(18.dp)
                )
                Spacer(Modifier.size(8.dp))
                Text(
                    text = title,
                    style = MaterialTheme.typography.titleSmall,
                    color = MaterialTheme.colorScheme.onSurface,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f)
                )
                ToolCardIconButton(Icons.Filled.Fullscreen, "全屏查看", onOpen)
                ToolCardIconButton(Icons.Filled.Star, "收藏", onCollect)
                ToolCardIconButton(Icons.Filled.Share, "分享", onShare)
            }

            Spacer(Modifier.height(8.dp))

            // Body: tap-to-open preview
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(8.dp))
                    .background(chatColors.codeBackground)
                    .clickable(onClick = onOpen)
                    .padding(10.dp)
            ) {
                Column {
                    previewLines.forEach { line ->
                        Text(
                            text = line,
                            style = MaterialTheme.typography.bodySmall.copy(
                                fontFamily = FontFamily.Monospace,
                                color = chatColors.onCodeBackground
                            ),
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis
                        )
                    }
                    if (totalLines > PREVIEW_LINES) {
                        Spacer(Modifier.size(4.dp))
                        Text(
                            text = "点击查看全部 ($totalLines 行)",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.primary
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun ToolCardIconButton(
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    description: String,
    onClick: () -> Unit
) {
    IconButton(onClick = onClick, modifier = Modifier.size(32.dp)) {
        Icon(
            imageVector = icon,
            contentDescription = description,
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.size(16.dp)
        )
    }
}

/**
 * Picks a sensible title for a tool card based on its content:
 *   - HTML: contents of the first `<title>…</title>` tag
 *   - Markdown: first `# heading`
 *   - Fallback: first non-blank line, trimmed to 40 chars
 */
fun deriveToolTitle(content: String, type: ToolType): String {
    if (type == ToolType.HTML) {
        val titleMatch = Regex("(?i)<title[^>]*>(.*?)</title>").find(content)
        val t = titleMatch?.groupValues?.getOrNull(1)?.trim()?.takeIf { it.isNotEmpty() }
        if (!t.isNullOrEmpty()) return t
    }
    val lines = content.lineSequence().filter { it.isNotBlank() }.toList()
    if (lines.isEmpty()) return "未命名工具"
    val heading = lines.firstOrNull { it.trimStart().startsWith("#") }
    if (heading != null) {
        return heading.trimStart { it == '#' }.trim().take(40)
    }
    return lines.first().take(40)
}

private const val PREVIEW_LINES = 8
