package com.example.aichat.ui.chat

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ExpandLess
import androidx.compose.material.icons.filled.ExpandMore
import androidx.compose.material.icons.filled.Psychology
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.example.aichat.domain.model.MessageMetadata

/**
 * Collapsible card showing the model's reasoning ("thinking") trace.
 *
 * Models that emit `reasoning_content` (DeepSeek-R1, Qwen3, GLM-Zero, Claude
 * 3.7 extended-thinking, o1, …) stream their chain-of-thought separately from
 * the final answer. This card surfaces that trace in a monospace block so
 * the user can audit / learn from the reasoning without it cluttering the
 * main answer.
 *
 * Default state: **collapsed** if reasoning is already complete, **expanded**
 * while streaming (so user sees the model "think" live).
 */
@Composable
fun ReasoningSection(
    reasoning: String,
    isStreaming: Boolean,
    modifier: Modifier = Modifier
) {
    if (reasoning.isBlank()) return

    var expanded by rememberSaveable(reasoning.hashCode()) { mutableStateOf(isStreaming) }
    // v4.2.2: cap the expanded reasoning to 240dp and give it its own
    // vertical scroll state. Two reasons:
    //   1. Long CoT traces (thinking models can emit 5k+ tokens) would
    //      otherwise push the actual answer off-screen for several viewports.
    //   2. While streaming, we want the reasoning to "follow itself" — the
    //      newest line should always be visible — without scrolling the
    //      parent chat list. By using an inner scroll state + an auto-follow
    //      LaunchedEffect, the reasoning block reads like a live log: the
    //      header stays put (sticky), the body grows + scrolls itself.
    val reasoningScroll = rememberScrollState()
    LaunchedEffect(reasoning, isStreaming, expanded) {
        if (expanded && isStreaming) {
            // Animate to bottom as new tokens arrive. maxValue is the scroll
            // range — it grows as content grows, so this always lands at the
            // newest line. Use animateScrollTo for a smooth follow; if the
            // user has manually scrolled up to read earlier reasoning, we
            // still scroll (cheap, and they re-position easily).
            reasoningScroll.animateScrollTo(reasoningScroll.maxValue)
        }
    }

    Surface(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(12.dp),
        color = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.6f),
        tonalElevation = 0.dp
    ) {
        Column(modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp)) {
            // Header — clickable, always visible at the top of the card.
            // Acts like a "sticky" title: while the body scrolls inside the
            // 240dp window below, this header stays anchored.
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(8.dp))
                    .clickable { expanded = !expanded }
                    .padding(vertical = 4.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Icon(
                    imageVector = Icons.Filled.Psychology,
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.secondary,
                    modifier = Modifier.size(16.dp)
                )
                Spacer(Modifier.width(6.dp))
                Text(
                    text = if (isStreaming) "思考中…" else "思考过程",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.weight(1f)
                )
                Icon(
                    imageVector = if (expanded) Icons.Filled.ExpandLess else Icons.Filled.ExpandMore,
                    contentDescription = if (expanded) "折叠" else "展开",
                    tint = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.size(20.dp)
                )
            }

            AnimatedVisibility(
                visible = expanded,
                enter = expandVertically() + fadeIn(),
                exit = shrinkVertically() + fadeOut()
            ) {
                Box(
                    modifier = Modifier
                        .padding(top = 6.dp)
                        .fillMaxWidth()
                        .heightIn(max = 240.dp)
                        .verticalScroll(reasoningScroll)
                ) {
                    Text(
                        text = reasoning,
                        style = MaterialTheme.typography.bodySmall.copy(
                            fontFamily = FontFamily.Monospace,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    )
                }
            }
        }
    }
}

/**
 * Collapsible list of web/document search results surfaced by RAG-enabled
 * profiles. Each item shows title + snippet + url (clickable later).
 *
 * Currently driven by [MessageMetadata.searchResults] — populated when the
 * backend emits a `search_results` event in the SSE stream.
 */
@Composable
fun SearchResultsSection(
    results: List<MessageMetadata.SearchResult>,
    modifier: Modifier = Modifier
) {
    if (results.isEmpty()) return

    var expanded by rememberSaveable { mutableStateOf(false) }

    Surface(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(12.dp),
        color = MaterialTheme.colorScheme.primary.copy(alpha = 0.08f),
        tonalElevation = 0.dp
    ) {
        Column(modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp)) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(8.dp))
                    .clickable { expanded = !expanded }
                    .padding(vertical = 4.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Icon(
                    imageVector = Icons.Filled.Search,
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.size(16.dp)
                )
                Spacer(Modifier.width(6.dp))
                Text(
                    text = "检索到 ${results.size} 条结果",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.weight(1f)
                )
                Icon(
                    imageVector = if (expanded) Icons.Filled.ExpandLess else Icons.Filled.ExpandMore,
                    contentDescription = if (expanded) "折叠" else "展开",
                    tint = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.size(20.dp)
                )
            }

            AnimatedVisibility(
                visible = expanded,
                enter = expandVertically() + fadeIn(),
                exit = shrinkVertically() + fadeOut()
            ) {
                Column(
                    modifier = Modifier.padding(top = 6.dp),
                    verticalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    results.forEach { result ->
                        SearchResultItem(result)
                    }
                }
            }
        }
    }
}

@Composable
private fun SearchResultItem(result: MessageMetadata.SearchResult) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(
                MaterialTheme.colorScheme.surface.copy(alpha = 0.6f),
                RoundedCornerShape(8.dp)
            )
            .padding(horizontal = 10.dp, vertical = 6.dp)
    ) {
        Text(
            text = result.title.ifBlank { result.url ?: "(无标题)" },
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurface,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis
        )
        if (result.snippet.isNotBlank()) {
            Spacer(Modifier.size(2.dp))
            Text(
                text = result.snippet,
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis
            )
        }
        if (!result.url.isNullOrBlank()) {
            Spacer(Modifier.size(2.dp))
            Text(
                text = result.url,
                style = MaterialTheme.typography.labelSmall.copy(fontFamily = FontFamily.Monospace),
                color = MaterialTheme.colorScheme.primary,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )
        }
    }
}
