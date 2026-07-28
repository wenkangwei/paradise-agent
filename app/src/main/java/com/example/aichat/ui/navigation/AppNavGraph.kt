package com.example.aichat.ui.navigation

import androidx.compose.runtime.Composable
import androidx.navigation.NavHostController
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.example.aichat.feature.profile.PlaceholderPage
import com.example.aichat.feature.profile.SettingsHomePage
import com.example.aichat.feature.profile.SettingsRoute
import com.example.aichat.feature.profile.apiconfig.ApiConfigEditPage
import com.example.aichat.feature.profile.apiconfig.ApiConfigListPage
import com.example.aichat.feature.voice.VoiceConfigPage
import com.example.aichat.ui.chat.ChatScreen

/**
 * Central route registry. Keep the string values in sync with navigate(...)
 * calls throughout the app.
 */
object Routes {
    const val CHAT = "chat"
    const val CHAT_WITH_CONVERSATION = "chat/{conversationId}"
    const val SETTINGS_HOME = "settings_home"

    fun chatRoute(conversationId: String? = null): String =
        if (conversationId != null) "chat/$conversationId" else CHAT
}

@Composable
fun AppNavGraph(
    navController: NavHostController = rememberNavController()
) {
    val back: () -> Unit = { navController.popBackStack() }

    NavHost(
        navController = navController,
        startDestination = Routes.CHAT
    ) {
        composable(Routes.CHAT) {
            ChatScreen(
                onNavigateToSettings = { navController.navigate(Routes.SETTINGS_HOME) }
            )
        }
        composable(
            route = Routes.CHAT_WITH_CONVERSATION,
            arguments = listOf(navArgument("conversationId") { type = NavType.StringType })
        ) {
            ChatScreen(
                onNavigateToSettings = { navController.navigate(Routes.SETTINGS_HOME) }
            )
        }

        // ---- Settings tree ----
        composable(Routes.SETTINGS_HOME) {
            SettingsHomePage(
                onBack = back,
                onNavigate = { route -> navController.navigate(route) }
            )
        }
        composable(SettingsRoute.API_CONFIG_LIST) {
            ApiConfigListPage(
                onBack = back,
                onAddNew = { navController.navigate(SettingsRoute.API_CONFIG_EDIT) },
                onEdit = { id -> navController.navigate("${SettingsRoute.API_CONFIG_EDIT}/$id") }
            )
        }
        composable(SettingsRoute.API_CONFIG_EDIT) {
            // create mode (no id)
            ApiConfigEditPage(
                profileId = null,
                onBack = back,
                onSaved = back
            )
        }
        composable(
            route = "${SettingsRoute.API_CONFIG_EDIT}/{id}",
            arguments = listOf(navArgument("id") { type = NavType.StringType })
        ) { backStackEntry ->
            ApiConfigEditPage(
                profileId = backStackEntry.arguments?.getString("id"),
                onBack = back,
                onSaved = back
            )
        }
        composable(SettingsRoute.VOICE_CONFIG) {
            VoiceConfigPage(onBack = back)
        }

        // Placeholder pages — each replaceable with a real implementation
        composable(SettingsRoute.PROFILE) { PlaceholderPage("个人资料", back) }
        composable(SettingsRoute.ACCOUNT_SECURITY) { PlaceholderPage("账号安全", back) }
        composable(SettingsRoute.AGENT_LIST) { PlaceholderPage("Agent 配置", back) }
        composable(SettingsRoute.APPEARANCE) { PlaceholderPage("主题设置", back) }
        composable(SettingsRoute.NOTIFICATIONS) { PlaceholderPage("消息通知", back) }
        composable(SettingsRoute.HELP) { PlaceholderPage("帮助中心", back) }
        composable(SettingsRoute.USAGE) { PlaceholderPage("使用说明", back) }
    }
}
