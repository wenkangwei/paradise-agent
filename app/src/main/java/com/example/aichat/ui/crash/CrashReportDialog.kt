package com.example.aichat.ui.crash

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import com.example.aichat.util.CrashInfo
import com.example.aichat.util.CrashReporter

/**
 * Modal dialog that surfaces the last persisted crash to the user on
 * the next successful launch.
 *
 * Buttons:
 *  - **复制** (Copy): writes the full report (metadata + stacktrace) to
 *    the system clipboard. The crash record is *kept* so the user can
 *    also tap "share" or come back later via app re-launch.
 *  - **清除记录** (Clear): removes the persisted crash so the next
 *    launch doesn't pop the dialog again.
 *  - **知道了** (Dismiss): closes the dialog but keeps the record, so
 *    the next launch will re-show it. Use this if you want to inspect
 *    it again later.
 *
 * The stacktrace is wrapped in a [SelectionContainer] so the user can
 * also select a portion directly in the dialog and copy just that.
 */
@Composable
fun CrashReportDialog(
    info: CrashInfo,
    onDismiss: () -> Unit,
    onClear: () -> Unit
) {
    val clipboard = LocalClipboardManager.current

    AlertDialog(
        onDismissRequest = onDismiss,
        title = {
            Text(
                text = "⚠️ 上次启动崩溃",
                style = MaterialTheme.typography.titleMedium
            )
        },
        text = {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(max = 480.dp)
                    .verticalScroll(rememberScrollState())
            ) {
                MetaRow("时间", CrashReporter.formatTime(info.time))
                MetaRow("进程", info.process)
                MetaRow("线程", info.thread)
                MetaRow("异常", info.throwableClass)
                if (!info.message.isNullOrEmpty()) {
                    MetaRow("消息", info.message)
                }

                Spacer(modifier = Modifier.padding(top = 12.dp))
                Text(
                    text = "Stacktrace:",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                Spacer(modifier = Modifier.padding(top = 4.dp))
                // SelectionContainer: lets the user drag-select a portion
                // of the stacktrace in addition to the "复制" button which
                // copies everything.
                SelectionContainer {
                    Text(
                        text = info.stacktrace,
                        style = MaterialTheme.typography.bodySmall.copy(
                            fontFamily = FontFamily.Monospace
                        ),
                        modifier = Modifier.fillMaxWidth()
                    )
                }
            }
        },
        confirmButton = {
            TextButton(onClick = onDismiss) { Text("知道了") }
        },
        dismissButton = {
            Row {
                TextButton(onClick = {
                    clipboard.setText(AnnotatedString(info.fullText()))
                    // Keep the record after copy so the user can also
                    // share or come back to it.
                    onDismiss()
                }) { Text("复制") }
                TextButton(onClick = onClear) { Text("清除记录") }
            }
        }
    )
}

@Composable
private fun MetaRow(label: String, value: String) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 2.dp)
    ) {
        Text(
            text = "$label: ",
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        Text(
            text = value,
            style = MaterialTheme.typography.bodySmall
        )
    }
}
