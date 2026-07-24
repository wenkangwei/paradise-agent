package com.example.aichat.data.repository

import android.content.Context
import com.example.aichat.data.local.dao.ApiProfileDao
import com.example.aichat.data.provider.BuiltinSuppliers
import com.example.aichat.data.remote.ConfigManager
import com.example.aichat.di.IoDispatcher
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.withContext
import java.util.UUID
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Idempotent first-run migration: if the [ApiProfileDao] table is empty AND the
 * legacy [ConfigManager] SharedPreferences has a non-default baseUrl/apiKey/model,
 * seed one [com.example.aichat.data.local.entity.ApiProfileEntity] from those values
 * so existing users keep their working setup.
 *
 * On a fresh install with no legacy config, seeds a profile pointing at the
 * default server (BuiltinSuppliers.fallback) so the app is usable out of the box.
 *
 * Runs once from [com.example.aichat.AiChatApp.onCreate] inside a non-blocking
 * coroutine; failures are swallowed (logged) so they never block app startup.
 */
@Singleton
class ApiProfileBootstrap @Inject constructor(
    @ApplicationContext private val context: Context,
    private val dao: ApiProfileDao,
    private val repo: ApiProfileRepository,
    @IoDispatcher private val ioDispatcher: CoroutineDispatcher
) {
    suspend fun ensureSeeded() = withContext(ioDispatcher) {
        if (dao.getAll().isNotEmpty()) return@withContext

        runCatching {
            val legacy = ConfigManager(context).currentConfig()
            val now = System.currentTimeMillis()

            // Heuristic: if baseUrl still equals the compiled default, treat the
            // install as fresh and seed the default server profile only.
            val isLegacyConfigured = legacy.baseUrl != DEFAULT_LEGACY_URL ||
                legacy.apiKey.isNotEmpty() ||
                legacy.model != DEFAULT_LEGACY_MODEL

            if (isLegacyConfigured) {
                val supplier = BuiltinSuppliers.byId(SUPPLIER_CUSTOM) ?: BuiltinSuppliers.fallback
                repo.upsert(
                    id = null,
                    title = "迁移配置",
                    supplierId = supplier.id,
                    baseUrl = legacy.baseUrl,
                    apiKey = legacy.apiKey,
                    modelName = legacy.model,
                    makeDefault = true
                )
            } else {
                val supplier = BuiltinSuppliers.fallback
                repo.upsert(
                    id = null,
                    title = supplier.displayName,
                    supplierId = supplier.id,
                    baseUrl = supplier.defaultBaseUrl,
                    apiKey = "",
                    modelName = supplier.suggestedModels.first(),
                    makeDefault = true
                )
            }
            // Silence the "unused" warning for `now` — kept here intentionally so
            // future migrations can backfill timestamps if the entity changes.
            @Suppress("UNUSED_VARIABLE") val _now = now
        }
    }

    companion object {
        // Must match the defaults in com.example.aichat.domain.model.AppConfig
        private const val DEFAULT_LEGACY_URL = "https://api.openai.com/v1/"
        private const val DEFAULT_LEGACY_MODEL = "gpt-4o-mini"
        private const val SUPPLIER_CUSTOM = "custom"

        @Suppress("unused")
        private fun newId(): String = UUID.randomUUID().toString()
    }
}
