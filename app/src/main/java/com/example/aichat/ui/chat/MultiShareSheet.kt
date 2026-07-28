package com.example.aichat.ui.chat

import android.content.Intent
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Share
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.example.aichat.ui.chat.model.ChatMessage
import com.example.aichat.ui.chat.model.Role
import kotlinx.coroutines.launch

/**
 * v4.2.12 #5: multi-select share sheet.
 *
 * Opens when the user taps the share icon on any bubble (or picks 分享
 * from the long-press menu). Pre-selects the message they tapped so the
 * "share just this one" case stays one extra tap away. The user can
 * check additional user/AI turns and tap 分享 to fire an Android
 * ACTION_SEND intent with all selected messages concatenated as a single
 * text block:
 *
 *   用户：question1
 *
 *   AI：answer1
 *
 *   用户：question2
 *
 *   AI：answer2
 *
 * Only messages with non-blank content are eligible — silent skips for
 * streaming partials and tool-only blobs keep the share preview clean.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MultiShareSheet(
    messages: List<ChatMessage>,
    initialSelectedIds: Set<String>,
    onDismiss: () -> Unit
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)

    // Mutable map of messageId → selected. Stable across recompositions
    // inside the sheet's lifetime. Initialized from the tapped bubble's id.
    val selected = remember {
        mutableStateMapOf<String, Boolean>().apply {
            initialSelectedIds.forEach { put(it, true) }
        }
    }

    // Only show messages that have actual text to share. Streaming
    // partials and attachment-only bubbles are excluded.
    val shareable = remember(messages) {
        messages.filter { it.content.isNotBlank() }
    }

    fun selectedCount() = selected.values.count { it }

    fun fireShare() {
        val chosen = shareable.filter { selected[it.id] == true }
        if (chosen.isEmpty()) return
        val text = buildSharedText(chosen)
        val intent = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_TEXT, text)
        }
        runCatching {
            context.startActivity(Intent.createChooser(intent, "分享 ${chosen.size} 条消息"))
        }
        scope.launch { sheetState.hide() }.invokeOnCompletion { onDismiss() }
    }

    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = sheetState
    ) {
        Column(modifier = Modifier.fillMaxWidth()) {
            // Header
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 20.dp, vertical = 8.dp),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Column {
                    Text(
                        text = "选择分享内容",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.SemiBold
                    )
                    Text(
                        text = "已选 ${selectedCount()} / ${shareable.size} 条",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
                Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                    TextButton(onClick = {
                        shareable.forEach { selected[it.id] = true }
                    }) { Text("全选") }
                    TextButton(onClick = { selected.clear() }) { Text("清空") }
                }
            }

            // Message list
            LazyColumn(
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(max = 420.dp)
            ) {
                items(items = shareable, key = { it.id }) { msg ->
                    val isChecked = selected[msg.id] == true
                    ShareRow(
                        message = msg,
                        checked = isChecked,
                        onToggle = { selected[msg.id] = !isChecked }
                    )
                }
            }

            // Sticky bottom share button
            Surface(tonalElevation = 2.dp) {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 20.dp, vertical = 12.dp),
                    horizontalArrangement = Arrangement.End,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Button(
                        onClick = { fireShare() },
                        enabled = selectedCount() > 0
                    ) {
                        Icon(
                            imageVector = Icons.Filled.Share,
                            contentDescription = null,
                            modifier = Modifier.size(18.dp)
                        )
                        Spacer(Modifier.size(8.dp))
                        Text("分享 ${selectedCount()} 条")
                    }
                }
            }
        }
    }
}

@Composable
private fun ShareRow(
    message: ChatMessage,
    checked: Boolean,
    onToggle: () -> Unit
) {
    val isUser = message.role == Role.USER
    val roleLabel = if (isUser) "用户" else "AI"
    val tint = if (isUser) MaterialTheme.colorScheme.primary
               else MaterialTheme.colorScheme.tertiary

    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onToggle)
            .padding(horizontal = 20.dp, vertical = 10.dp),
        verticalAlignment = Alignment.Top
    ) {
        Checkbox(
            checked = checked,
            onCheckedChange = { onToggle() },
            modifier = Modifier.size(24.dp)
        )
        Spacer(Modifier.size(12.dp))
        Column(modifier = Modifier.fillMaxWidth()) {
            Text(
                text = roleLabel,
                style = MaterialTheme.typography.labelSmall,
                color = tint,
                fontWeight = FontWeight.SemiBold
            )
            Spacer(Modifier.size(2.dp))
            Text(
                text = message.content,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurface,
                maxLines = 3,
                overflow = TextOverflow.Ellipsis
            )
        }
    }
}

/**
 * Build the shared text block. Role-tagged, blank-line separated.
 * Order is preserved (oldest → newest) so the recipient can read the
 * conversation top-to-bottom.
 */
private fun buildSharedText(messages: List<ChatMessage>): String =
    messages.joinToString("\n\n") { msg ->
        val role = if (msg.role == Role.USER) "用户" else "AI"
        val body = msg.content.trim()
        "$role：$body"
    }
