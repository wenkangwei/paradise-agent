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
        //
        // v4.0: the STREAMING dangling-row sweep that used to live here has
        // moved to ChatViewModel.init. Reason: Application.onCreate fires in
        // *every* process (main AND :streaming), so the sweep was running
        // twice on cold start and could race with the :streaming process's
        // own UseCase persist loop. The ViewModel init only fires in the
        // main process, when the user actually opens the chat screen.
        appScope.launch { runCatching { bootstrap.ensureSeeded() } }
    }
}
