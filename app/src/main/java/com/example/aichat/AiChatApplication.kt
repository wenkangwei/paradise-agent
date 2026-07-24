package com.example.aichat

import android.app.Application
import com.example.aichat.data.repository.ApiProfileBootstrap
import dagger.hilt.android.HiltAndroidApp
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import javax.inject.Inject

@HiltAndroidApp
class AiChatApplication : Application() {

    @Inject lateinit var bootstrap: ApiProfileBootstrap

    private val appScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun onCreate() {
        super.onCreate()
        // Seed default ApiProfile on first launch or migrate legacy ConfigManager
        // values — failures are swallowed inside ensureSeeded() so they never
        // crash app startup.
        appScope.launch { runCatching { bootstrap.ensureSeeded() } }
    }
}
