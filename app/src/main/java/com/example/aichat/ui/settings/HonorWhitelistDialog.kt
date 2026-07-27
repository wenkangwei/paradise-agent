package com.example.aichat.ui.settings

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.height
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.example.aichat.util.HonorOemHelper

/**
 * v4.2.11: One-time prompt shown on first launch of a Honor/Huawei device
 * explaining that lock-screen AI streaming will be killed by Honor's
 * PGManager unless the user adds this app to the system's App Launch
 * Management whitelist.
 *
 * The "打开应用启动管理" button launches the Honor settings page directly
 * via [HonorOemHelper.openAppLaunchManagement].
 *
 * Why this exists: logcat on Honor MagicOS showed Pged destroying our SSE
 * socket ~1.3s after screen-off (see commit log v4.2.11). Code-side keep-
 * alives cannot prevent this; only the user can whitelist the app.
 */
@Composable
fun HonorWhitelistDialog(
    onDismiss: () -> Unit
) {
    val context = LocalContext.current
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("检测到 Honor 设备") },
        text = {
            Column {
                Text(
                    "为保证锁屏后 AI 回复不被中断，需要将本应用加入" +
                        "「应用启动管理」白名单。"
                )
                Spacer(Modifier.height(12.dp))
                Text(
                    "操作步骤：",
                    style = MaterialTheme.typography.bodySmall,
                    fontWeight = FontWeight.Bold
                )
                Spacer(Modifier.height(4.dp))
                Text("1. 点击下方按钮进入系统设置", style = MaterialTheme.typography.bodySmall)
                Text("2. 找到 AiChat，关闭「自动管理」", style = MaterialTheme.typography.bodySmall)
                Text(
                    "3. 手动开启「允许自启动 / 关联启动 / 后台活动」",
                    style = MaterialTheme.typography.bodySmall
                )
            }
        },
        confirmButton = {
            Button(onClick = {
                HonorOemHelper.openAppLaunchManagement(context)
                onDismiss()
            }) {
                Text("打开应用启动管理")
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("稍后")
            }
        }
    )
}
