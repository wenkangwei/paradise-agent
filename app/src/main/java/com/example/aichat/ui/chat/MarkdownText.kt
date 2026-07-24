package com.example.aichat.ui.chat

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.example.aichat.ui.theme.chatColors

/**
 * Simple Markdown renderer using AnnotatedString.
 * Supports: code blocks (```), inline code (`), bold (**), and line breaks.
 */
@Composable
fun MarkdownText(
    text: String,
    modifier: Modifier = Modifier,
    style: androidx.compose.ui.text.TextStyle = MaterialTheme.typography.bodyMedium
) {
    val segments = parseMarkdown(text)
    val hasCodeBlock = segments.any { it is MarkdownSegment.CodeBlock }

    if (hasCodeBlock) {
        Column(modifier = modifier) {
            segments.forEach { segment ->
                when (segment) {
                    is MarkdownSegment.CodeBlock -> {
                        Text(
                            text = segment.content,
                            modifier = Modifier
                                .fillMaxWidth()
                                .clip(RoundedCornerShape(8.dp))
                                .background(MaterialTheme.chatColors.codeBackground)
                                .padding(12.dp),
                            style = androidx.compose.ui.text.TextStyle(
                                fontFamily = FontFamily.Monospace,
                                fontSize = 13.sp,
                                lineHeight = 18.sp,
                                color = MaterialTheme.chatColors.onCodeBackground
                            )
                        )
                    }
                    is MarkdownSegment.Text -> {
                        Text(
                            text = parseInlineMarkdown(segment.content),
                            style = style,
                            color = MaterialTheme.colorScheme.onSurface
                        )
                    }
                }
            }
        }
    } else {
        Text(
            text = parseInlineMarkdown(text),
            modifier = modifier,
            style = style
        )
    }
}

private sealed class MarkdownSegment {
    data class Text(val content: String) : MarkdownSegment()
    data class CodeBlock(val content: String) : MarkdownSegment()
}

private fun parseMarkdown(text: String): List<MarkdownSegment> {
    val segments = mutableListOf<MarkdownSegment>()
    val codeBlockRegex = Regex("```(\\w*)\\n?([\\s\\S]*?)```")
    var lastIndex = 0

    codeBlockRegex.findAll(text).forEach { match ->
        if (match.range.first > lastIndex) {
            segments.add(MarkdownSegment.Text(text.substring(lastIndex, match.range.first)))
        }
        val codeContent = match.groupValues[2].trimEnd()
        segments.add(MarkdownSegment.CodeBlock(codeContent))
        lastIndex = match.range.last + 1
    }

    if (lastIndex < text.length) {
        segments.add(MarkdownSegment.Text(text.substring(lastIndex)))
    }

    return segments
}

private fun parseInlineMarkdown(text: String): AnnotatedString {
    val combinedRegex = Regex("\\*\\*(.+?)\\*\\*|`([^`]+?)`")
    return buildAnnotatedString {
        var lastEnd = 0

        combinedRegex.findAll(text).forEach { match ->
            if (match.range.first > lastEnd) {
                append(text.substring(lastEnd, match.range.first))
            }

            when {
                match.value.startsWith("**") -> {
                    withStyle(SpanStyle(fontWeight = FontWeight.Bold)) {
                        append(match.groupValues[1])
                    }
                }
                match.value.startsWith("`") -> {
                    withStyle(SpanStyle(fontFamily = FontFamily.Monospace, fontSize = 13.sp)) {
                        append(match.groupValues[2])
                    }
                }
                else -> append(match.value)
            }
            lastEnd = match.range.last + 1
        }

        if (lastEnd < text.length) {
            append(text.substring(lastEnd))
        }
    }
}
