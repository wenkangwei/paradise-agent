package com.example.aichat

import android.content.Context
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.Box
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import com.example.aichat.ui.crash.CrashReportHost
import com.example.aichat.ui.navigation.AppNavGraph
import com.example.aichat.ui.settings.HonorWhitelistDialog
import com.example.aichat.ui.theme.AiChatTheme
import com.example.aichat.util.HonorOemHelper
import dagger.hilt.android.AndroidEntryPoint

@AndroidEntryPoint
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        // v4.2.11: On first launch on a Honor/Huawei device, prompt the
        // user to add this app to the system's App Launch Management
        // whitelist. logcat on Honor MagicOS proved that PGManager
        // destroys our SSE socket ~1.3s after screen-off (kernel-level
        // cgroup freeze), which no app-side keep-alive can prevent.
        // See HonorOemHelper for the full backstory.
        val prefs = getSharedPreferences(HONOR_SETUP_PREFS, Context.MODE_PRIVATE)
        val shouldShowHonorPrompt = savedInstanceState == null &&
            HonorOemHelper.isHonorOrHuawei() &&
            !prefs.getBoolean(HONOR_WHITELIST_PROMPTED_KEY, false)
        if (shouldShowHonorPrompt) {
            prefs.edit().putBoolean(HONOR_WHITELIST_PROMPTED_KEY, true).apply()
        }

        setContent {
            AiChatTheme {
                var showHonorDialog by remember { mutableStateOf(shouldShowHonorPrompt) }

                Box {
                    AppNavGraph()
                    CrashReportHost()
                }

                if (showHonorDialog) {
                    HonorWhitelistDialog(onDismiss = { showHonorDialog = false })
                }
            }
        }
    }

    private companion object {
        private const val HONOR_SETUP_PREFS = "honor_oem_setup"
        private const val HONOR_WHITELIST_PROMPTED_KEY = "whitelist_prompted_v1"
    }
}
