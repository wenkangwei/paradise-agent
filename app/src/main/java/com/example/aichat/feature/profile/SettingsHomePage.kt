package com.example.aichat.feature.profile

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
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowRight
import androidx.compose.material.icons.filled.AccountCircle
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp

/**
 * Top-level settings entry. Renders the user's avatar header plus three
 * groups of navigation rows. Each row navigates to a sub-page via [onNavigate].
 *
 * Routes are kept as plain strings (in [SettingsRoute]) so new pages can be
 * added by appending a constant + a branch in the NavGraph.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsHomePage(
    onBack: () -> Unit,
    onNavigate: (String) -> Unit
) {
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("设置") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(
                            imageVector = Icons.AutoMirrored.Filled.ArrowBack,
                            contentDescription = "返回"
                        )
                    }
                }
            )
        }
    ) { padding ->
        LazyColumn(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding),
            contentPadding = PaddingValues(vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            item { UserHeader() }

            item {
                GroupLabel("个人信息")
            }
            items(
                items = listOf(
                    SettingsItem("个人资料", SettingsRoute.PROFILE),
                    SettingsItem("账号安全", SettingsRoute.ACCOUNT_SECURITY)
                ),
                key = { it.route }
            ) { item ->
                SettingsRow(item, onClick = { onNavigate(item.route) })
            }

            item {
                HorizontalDivider()
                GroupLabel("个性化")
            }
            items(
                items = listOf(
                    SettingsItem("模型 API 配置", SettingsRoute.API_CONFIG_LIST),
                    SettingsItem("Agent 配置", SettingsRoute.AGENT_LIST)
                ),
                key = { it.route }
            ) { item ->
                SettingsRow(item, onClick = { onNavigate(item.route) })
            }

            item {
                HorizontalDivider()
                GroupLabel("通用")
            }
            items(
                items = listOf(
                    SettingsItem("主题设置", SettingsRoute.APPEARANCE),
                    SettingsItem("消息通知", SettingsRoute.NOTIFICATIONS),
                    SettingsItem("帮助中心", SettingsRoute.HELP),
                    SettingsItem("使用说明", SettingsRoute.USAGE)
                ),
                key = { it.route }
            ) { item ->
                SettingsRow(item, onClick = { onNavigate(item.route) })
            }
        }
    }
}

@Composable
private fun UserHeader() {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 20.dp, vertical = 16.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Box(
            modifier = Modifier
                .size(64.dp)
                .clip(CircleShape),
            contentAlignment = Alignment.Center
        ) {
            Icon(
                imageVector = Icons.Filled.AccountCircle,
                contentDescription = null,
                tint = MaterialTheme.colorScheme.primary,
                modifier = Modifier.size(64.dp)
            )
        }
        Spacer(Modifier.size(16.dp))
        Column {
            Text(
                text = "未登录",
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Medium
            )
            Text(
                text = "点击登录以同步配置",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
    }
}

@Composable
private fun GroupLabel(text: String) {
    Text(
        text = text,
        style = MaterialTheme.typography.labelLarge,
        color = MaterialTheme.colorScheme.primary,
        modifier = Modifier.padding(horizontal = 20.dp, vertical = 4.dp)
    )
}

@Composable
private fun SettingsRow(
    item: SettingsItem,
    onClick: () -> Unit
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
            .padding(horizontal = 20.dp, vertical = 14.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(
            text = item.title,
            style = MaterialTheme.typography.bodyLarge,
            modifier = Modifier.weight(1f)
        )
        Icon(
            imageVector = Icons.AutoMirrored.Filled.KeyboardArrowRight,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.onSurfaceVariant
        )
    }
}

private data class SettingsItem(val title: String, val route: String)

/** All routes reachable from the settings home. */
object SettingsRoute {
    const val PROFILE = "settings/profile"
    const val ACCOUNT_SECURITY = "settings/account_security"
    const val API_CONFIG_LIST = "settings/api_config"
    const val API_CONFIG_EDIT = "settings/api_config/edit" // optional /{id}
    const val AGENT_LIST = "settings/agents"
    const val APPEARANCE = "settings/appearance"
    const val NOTIFICATIONS = "settings/notifications"
    const val HELP = "settings/help"
    const val USAGE = "settings/usage"
}
