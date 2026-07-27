package com.example.aichat.ui.crash

import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import com.example.aichat.util.CrashInfo
import com.example.aichat.util.CrashReporter

/**
 * Top-level host composable that surfaces any crash persisted by
 * [CrashReporter] from the previous launch.
 *
 * Mount this once at the root of the Compose tree (inside the theme,
 * wrapping or alongside the main nav graph). It performs no UI when
 * there is no pending crash, so it's cheap to leave mounted.
 *
 * Lifecycle:
 *  - On first composition, reads [CrashReporter.loadLastCrash].
 *  - If present, shows [CrashReportDialog].
 *  - "知道了" → state cleared, dialog hidden, **record kept** (re-launch
 *    will show it again until explicitly cleared).
 *  - "清除记录" → [CrashReporter.clearLastCrash] called, dialog hidden,
 *    re-launch won't show it.
 */
@Composable
fun CrashReportHost() {
    val context = LocalContext.current
    var crashInfo by remember { mutableStateOf<CrashInfo?>(null) }

    LaunchedEffect(Unit) {
        crashInfo = CrashReporter.loadLastCrash(context)
    }

    crashInfo?.let { info ->
        CrashReportDialog(
            info = info,
            onDismiss = { crashInfo = null },
            onClear = {
                CrashReporter.clearLastCrash(context)
                crashInfo = null
            }
        )
    }
}
