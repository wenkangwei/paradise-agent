package com.example.aichat

import android.app.Application
import com.example.aichat.data.repository.ApiProfileBootstrap
import com.example.aichat.domain.repository.ChatRepository
import dagger.hilt.android.HiltAndroidApp
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import javax.inject.Inject

@HiltAndroidApp
class AiChatApplication : Application() {

    @Inject lateinit var bootstrap: ApiProfileBootstrap
    @Inject lateinit var chatRepository: ChatRepository

    private val appScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun onCreate() {
        super.onCreate()
        // Seed default ApiProfile on first launch or migrate legacy ConfigManager
        // values — failures are swallowed inside ensureSeeded() so they never
        // crash app startup.
        appScope.launch { runCatching { bootstrap.ensureSeeded() } }
        // Sweep any messages the :streaming process left in STREAMING when it
        // was killed (OOM, swipe-away, etc.). Otherwise the UI would pick up
        // a stale STREAMING row on next observe and spin forever.
        appScope.launch { runCatching { chatRepository.markDanglingStreamingInterrupted("process_killed") } }
    }
}
