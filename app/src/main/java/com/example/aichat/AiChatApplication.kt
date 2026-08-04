package com.example.aichat

import android.app.ActivityManager
import android.app.Application
import androidx.hilt.work.HiltWorkerFactory
import androidx.work.Configuration
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.WorkManager
import com.example.aichat.data.repository.ApiProfileBootstrap
import com.example.aichat.service.FeedbackSyncWorker
import com.example.aichat.service.ProactivePollingWorker
import com.example.aichat.util.CrashReporter
import dagger.hilt.android.HiltAndroidApp
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import javax.inject.Inject

@HiltAndroidApp
class AiChatApplication : Application(), Configuration.Provider {

    @Inject lateinit var bootstrap: ApiProfileBootstrap
    @Inject lateinit var workerFactory: HiltWorkerFactory

    private val appScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override val workManagerConfiguration: Configuration
        get() = Configuration.Builder()
            .setWorkerFactory(workerFactory)
            .build()

    override fun onCreate() {
        super.onCreate()
        CrashReporter.install(this)
        appScope.launch { runCatching { bootstrap.ensureSeeded() } }

        // Only schedule workers on the main process.
        // The :streaming process has a stripped Hilt graph (no MainActivity
        // module) — HiltWorkerFactory injection would crash it.
        if (isMainProcess()) {
            WorkManager.getInstance(this).enqueueUniquePeriodicWork(
                FeedbackSyncWorker.WORK_NAME,
                ExistingPeriodicWorkPolicy.KEEP,
                FeedbackSyncWorker.buildRequest()
            )
            WorkManager.getInstance(this).enqueueUniquePeriodicWork(
                ProactivePollingWorker.WORK_NAME,
                ExistingPeriodicWorkPolicy.KEEP,
                ProactivePollingWorker.buildRequest()
            )
        }
    }

    private fun isMainProcess(): Boolean {
        val pid = android.os.Process.myPid()
        val am = getSystemService(ACTIVITY_SERVICE) as? ActivityManager ?: return false
        val processes = am.runningAppProcesses ?: return false
        val myName = processes.firstOrNull { it.pid == pid }?.processName ?: return false
        return myName == packageName
    }
}
