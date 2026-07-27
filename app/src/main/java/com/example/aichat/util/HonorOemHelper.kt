package com.example.aichat.util

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings

/**
 * v4.2.11: Honor / Huawei OEM detection + system-settings launcher.
 *
 * Real background story (see git log v4.2.7-v4.2.10):
 * - WakeLock + WifiLock + FGS notification importance + HTTP/2 ping all
 *   FAILED to prevent lock-screen SSE interruption on Honor MagicOS.
 * - logcat proof: Honor's PGManager (Power Guardian) freezes the app's
 *   processes and destroys its sockets from the kernel cgroup layer
 *   ~1.3s after screen-off (`Pged: Destroyed N sockets for uid:...`).
 * - This is below the application layer, so no app-side keep-alive can
 *   prevent it. The only effective fix is to add the app to Honor's
 *   user-level "App Launch Management" whitelist, which makes PGManager
 *   skip the freeze + socket-destroy for our uid.
 *
 * Kimi (com.moonshot.kimichat) bypasses this via Honor's vendor-side
 * pre-installed whitelist (not via app code). We can't replicate that;
 * we can only guide the user to the right settings page.
 */
object HonorOemHelper {

    /**
     * True if running on Honor MagicOS or Huawei EMUI. Both derive from
     * the same codebase and apply similar aggressive power management.
     */
    fun isHonorOrHuawei(): Boolean {
        val manufacturer = Build.MANUFACTURER?.lowercase()?.trim().orEmpty()
        val brand = Build.BRAND?.lowercase()?.trim().orEmpty()
        return manufacturer.contains("honor") || manufacturer.contains("huawei") ||
               brand.contains("honor") || brand.contains("huawei")
    }

    /**
     * Launch Honor's "应用启动管理" (App Launch Management) settings page
     * so the user can manually disable "自动管理" for this app and enable
     * the three sub-toggles (自启动 / 关联启动 / 后台活动).
     *
     * Tries several known ComponentNames in order; falls back to the
     * generic app-details settings page if all Honor-specific intents
     * fail to resolve.
     *
     * Returns true if any intent successfully launched.
     */
    fun openAppLaunchManagement(context: Context): Boolean {
        val candidates = listOf(
            // Honor MagicOS 6+/7+: protected-app list (alias 1)
            Intent().apply {
                component = ComponentName(
                    "com.hihonor.systemmanager",
                    "com.hihonor.systemmanager.optimize.process.ProtectActivity"
                )
            },
            // Honor MagicOS alternate entry (startup manager)
            Intent().apply {
                component = ComponentName(
                    "com.hihonor.systemmanager",
                    "com.hihonor.systemmanager.startupmgr.ui.StartupNormalAppListActivity"
                )
            },
            // Honor SystemManager main entry — user navigates from here
            Intent().apply {
                component = ComponentName(
                    "com.hihonor.systemmanager",
                    "com.hihonor.systemmanager.main.MainActivity"
                )
            },
            // Huawei EMUI (older devices, pre-Honor-spinoff)
            Intent().apply {
                component = ComponentName(
                    "com.huawei.systemmanager",
                    "com.huawei.systemmanager.optimize.process.ProtectActivity"
                )
            },
            // Huawei EMUI startup manager
            Intent().apply {
                component = ComponentName(
                    "com.huawei.systemmanager",
                    "com.huawei.systemmanager.startupmgr.ui.StartupNormalAppListActivity"
                )
            }
        )

        for (intent in candidates) {
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            runCatching {
                context.startActivity(intent)
                return true
            }
        }

        // Last-resort fallback: open this app's system details page so
        // the user can navigate to battery / startup settings manually.
        return runCatching {
            val fallback = Intent(
                Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                Uri.fromParts("package", context.packageName, null)
            ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            context.startActivity(fallback)
            true
        }.getOrDefault(false)
    }
}
