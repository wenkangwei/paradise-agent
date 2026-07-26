package com.example.aichat.ui.chat

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.expandVertically
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AccountCircle
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Bookmark
import androidx.compose.material.icons.filled.Chat
import androidx.compose.material.icons.filled.ChevronRight
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalDrawerSheet
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.example.aichat.data.repository.FavoriteTool
import com.example.aichat.domain.model.Conversation

/**
 * Drawer content: user header → new-chat button → collapsible "收藏工具"
 * section → collapsible "历史对话" section.
 *
 * Layout decision:
 *   - 收藏工具 defaults to collapsed so an empty favorites list doesn't
 *     take up drawer real estate on first install. The first time a tool
 *     is pinned, the caller can flip [favoritesInitialExpanded] to surface it.
 *   - 历史对话 defaults to expanded — that's the primary entry.
 */
@Composable
fun ChatListDrawer(
    conversations: List<Conversation>,
    currentConversationId: String?,
    favoriteTools: List<FavoriteTool>,
    onNewChat: () -> Unit,
    onSelectConversation: (String) -> Unit,
    onDeleteConversation: (String) -> Unit,
    onUseFavoriteTool: (FavoriteTool) -> Unit,
    onDeleteFavoriteTool: (String) -> Unit,
    onOpenSettings: () -> Unit = {},
    /** ConversationIds currently being streamed — rendered as a green dot. */
    streamingConversationIds: Set<String> = emptySet(),
    favoritesInitialExpanded: Boolean = false,
    modifier: Modifier = Modifier
) {
    var favoritesExpanded by rememberSaveable { mutableStateOf(favoritesInitialExpanded) }
    var historyExpanded by rememberSaveable { mutableStateOf(true) }

    ModalDrawerSheet(modifier = modifier) {
        // User header — clickable to open settings
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clickable(onClick = onOpenSettings)
                .padding(horizontal = 20.dp, vertical = 16.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Box(
                modifier = Modifier
                    .size(48.dp)
                    .clip(CircleShape),
                contentAlignment = Alignment.Center
            ) {
                Icon(
                    imageVector = Icons.Filled.AccountCircle,
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.size(48.dp)
                )
            }
            Spacer(Modifier.size(12.dp))
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = "未登录",
                    style = MaterialTheme.typography.titleSmall,
                    fontWeight = FontWeight.Medium
                )
                Text(
                    text = "点按进入设置",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            Icon(
                imageVector = Icons.Filled.Settings,
                contentDescription = "设置",
                tint = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }

        Box(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 4.dp)
        ) {
            Button(
                onClick = onNewChat,
                modifier = Modifier.fillMaxWidth()
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(imageVector = Icons.Filled.Add, contentDescription = null)
                    Spacer(Modifier.size(8.dp))
                    Text("新建对话")
                }
            }
        }

        if (conversations.isEmpty() && favoriteTools.isEmpty()) {
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(32.dp),
                contentAlignment = Alignment.Center
            ) {
                Text(
                    text = "暂无历史对话\n\n发送一条消息开始对话；\nAI 回复中的 HTML/长文档可点 ⭐ 收藏",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    textAlign = TextAlign.Center
                )
            }
        } else {
            LazyColumn(
                modifier = Modifier.fillMaxSize(),
                contentPadding = PaddingValues(horizontal = 12.dp, vertical = 4.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp)
            ) {
                // ---- 收藏工具 section ----
                item(key = "favorites_header") {
                    DrawerSectionHeader(
                        icon = Icons.Filled.Bookmark,
                        title = "收藏工具",
                        count = favoriteTools.size,
                        expanded = favoritesExpanded,
                        onToggle = { favoritesExpanded = !favoritesExpanded }
                    )
                }
                if (favoritesExpanded && favoriteTools.isNotEmpty()) {
                    items(
                        items = favoriteTools,
                        key = { "fav_${it.id}" }
                    ) { tool ->
                        FavoriteToolItem(
                            tool = tool,
                            onClick = { onUseFavoriteTool(tool) },
                            onDelete = { onDeleteFavoriteTool(tool.id) }
                        )
                    }
                }

                // ---- 历史对话 section ----
                item(key = "history_header") {
                    DrawerSectionHeader(
                        icon = Icons.Filled.Chat,
                        title = "历史对话",
                        count = conversations.size,
                        expanded = historyExpanded,
                        onToggle = { historyExpanded = !historyExpanded }
                    )
                }
                if (historyExpanded && conversations.isNotEmpty()) {
                    items(
                        items = conversations,
                        key = { it.id }
                    ) { conversation ->
                        ConversationItem(
                            conversation = conversation,
                            isSelected = conversation.id == currentConversationId,
                            onClick = { onSelectConversation(conversation.id) },
                            onDelete = { onDeleteConversation(conversation.id) },
                            isStreaming = conversation.id in streamingConversationIds
                        )
                    }
                }
            }
        }
    }
}

/**
 * Collapsible section header. The chevron rotates 90° when expanded.
 */
@Composable
private fun DrawerSectionHeader(
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    title: String,
    count: Int,
    expanded: Boolean,
    onToggle: () -> Unit,
    modifier: Modifier = Modifier
) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .clickable(onClick = onToggle)
            .padding(horizontal = 16.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Icon(
            imageVector = icon,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.size(18.dp)
        )
        Spacer(Modifier.size(8.dp))
        Text(
            text = title,
            style = MaterialTheme.typography.titleSmall,
            fontWeight = FontWeight.Medium,
            color = MaterialTheme.colorScheme.onSurface,
            modifier = Modifier.weight(1f)
        )
        if (count > 0) {
            Text(
                text = count.toString(),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier
                    .clip(CircleShape)
                    .background(MaterialTheme.colorScheme.surfaceVariant)
                    .padding(horizontal = 8.dp, vertical = 2.dp)
            )
            Spacer(Modifier.size(8.dp))
        }
        Icon(
            imageVector = Icons.Filled.ChevronRight,
            contentDescription = if (expanded) "收起" else "展开",
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier
                .size(20.dp)
                .rotate(if (expanded) 90f else 0f)
        )
    }
}
