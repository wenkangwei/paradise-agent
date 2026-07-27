package com.example.aichat

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.Box
import com.example.aichat.ui.crash.CrashReportHost
import com.example.aichat.ui.navigation.AppNavGraph
import com.example.aichat.ui.theme.AiChatTheme
import dagger.hilt.android.AndroidEntryPoint

@AndroidEntryPoint
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            AiChatTheme {
                // Box wrapper so CrashReportHost overlays on top of the
                // nav graph. Host renders nothing when there's no pending
                // crash, so existing UI is unaffected.
                Box {
                    AppNavGraph()
                    CrashReportHost()
                }
            }
        }
    }
}
