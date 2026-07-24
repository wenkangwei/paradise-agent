package com.example.aichat.data.repository

import com.example.aichat.data.local.dao.ApiProfileDao
import com.example.aichat.data.local.entity.ApiProfileEntity
import com.example.aichat.data.provider.BuiltinSuppliers
import com.example.aichat.data.provider.SupplierRegistry
import com.example.aichat.data.security.ApiKeyEncryptor
import com.example.aichat.di.IoDispatcher
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.withContext
import java.util.UUID
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Domain-side view of an [ApiProfileEntity] — without the encrypted key material.
 * UI layers (Api Config list / edit pages) consume this so the raw key never
 * crosses the data layer boundary.
 */
data class ApiProfile(
    val id: String,
    val title: String,
    val supplierId: String,
    val baseUrl: String,
    val modelName: String,
    val isDefault: Boolean,
    val hasApiKey: Boolean,
    val createdAt: Long,
    val updatedAt: Long
)

/**
 * CRUD + activation for [ApiProfileEntity].
 *
 * Responsibilities:
 *  - Translate between [ApiProfileEntity] (with ciphertext) and [ApiProfile] (without).
 *  - Delegate encryption/decryption to [ApiKeyEncryptor].
 *  - Maintain the invariant "exactly one row has isDefault=true" via [ApiProfileDao.setDefault].
 *  - Provide the active profile as a Flow so feature code can react to switches.
 */
interface ApiProfileRepository {
    fun observeAll(): Flow<List<ApiProfile>>
    fun observeActive(): Flow<ApiProfile?>
    suspend fun getAll(): List<ApiProfile>
    suspend fun getById(id: String): ApiProfile?
    suspend fun getActive(): ApiProfile?

    /**
     * Creates or updates a profile. When [makeDefault] is true (or the table was
     * previously empty), the saved row becomes the active profile.
     *
     * @param apiKey plaintext key; encrypted before persistence. Pass empty string
     *   to preserve the existing key on updates.
     */
    suspend fun upsert(
        id: String?,
        title: String,
        supplierId: String,
        baseUrl: String,
        apiKey: String,
        modelName: String,
        makeDefault: Boolean = false
    ): String

    suspend fun delete(id: String)
    suspend fun setActive(id: String)

    /**
     * Returns plaintext API keys previously stored for profiles of the given
     * [supplierId]. Used by the edit form to render a "history" dropdown so
     * the user doesn't have to retype a key they already entered elsewhere.
     * De-duplicated; empty keys excluded.
     */
    suspend fun keyHistoryForSupplier(supplierId: String): List<String>

    /**
     * Returns all distinct baseUrls ever stored for the given [supplierId] (or
     * across all profiles when supplierId is null). Used to pre-fill the URL
     * field's history dropdown.
     */
    suspend fun urlHistory(supplierId: String? = null): List<String>
}

@Singleton
class ApiProfileRepositoryImpl @Inject constructor(
    private val dao: ApiProfileDao,
    private val encryptor: ApiKeyEncryptor,
    private val supplierRegistry: SupplierRegistry,
    @IoDispatcher private val ioDispatcher: CoroutineDispatcher
) : ApiProfileRepository {

    override fun observeAll(): Flow<List<ApiProfile>> =
        dao.observeAll().map { list -> list.map { it.toDomain() } }

    override fun observeActive(): Flow<ApiProfile?> =
        dao.observeDefault().map { it?.toDomain() }

    override suspend fun getAll(): List<ApiProfile> = withContext(ioDispatcher) {
        dao.getAll().map { it.toDomain() }
    }

    override suspend fun getById(id: String): ApiProfile? = withContext(ioDispatcher) {
        dao.getById(id)?.toDomain()
    }

    override suspend fun getActive(): ApiProfile? = withContext(ioDispatcher) {
        dao.getDefault()?.toDomain()
    }

    override suspend fun upsert(
        id: String?,
        title: String,
        supplierId: String,
        baseUrl: String,
        apiKey: String,
        modelName: String,
        makeDefault: Boolean
    ): String = withContext(ioDispatcher) {
        val now = System.currentTimeMillis()
        val effectiveId = id ?: UUID.randomUUID().toString()

        // Resolve baseUrl/model from the Supplier if caller left them blank
        val supplier = supplierRegistry.byId(supplierId) ?: BuiltinSuppliers.fallback
        val resolvedBase = baseUrl.ifBlank { supplier.defaultBaseUrl }
        val resolvedModel = modelName.ifBlank { supplier.suggestedModels.firstOrNull().orEmpty() }

        // Preserve existing key when caller passed empty on an update
        val existing = id?.let { dao.getById(it) }
        val cipherText = when {
            apiKey.isNotEmpty() -> encryptor.encrypt(effectiveId, apiKey)
            existing != null -> existing.apiKeyEncrypted
            else -> ""
        }

        val isFirst = dao.getAll().isEmpty()
        val entity = ApiProfileEntity(
            id = effectiveId,
            title = title.ifBlank { supplier.displayName },
            supplierId = supplier.id,
            baseUrl = resolvedBase,
            apiKeyEncrypted = cipherText,
            modelName = resolvedModel,
            isDefault = existing?.isDefault ?: isFirst || makeDefault,
            customFieldsJson = "{}",
            createdAt = existing?.createdAt ?: now,
            updatedAt = now
        )
        dao.upsert(entity)

        if (isFirst || makeDefault) {
            dao.setDefault(effectiveId)
        }
        effectiveId
    }

    override suspend fun delete(id: String) = withContext(ioDispatcher) {
        val wasDefault = dao.getById(id)?.isDefault == true
        dao.deleteById(id)
        encryptor.forget(id)
        // Promote the oldest surviving row so an active profile always exists
        if (wasDefault) dao.getAll().firstOrNull()?.let { dao.setDefault(it.id) }
    }

    override suspend fun setActive(id: String) = withContext(ioDispatcher) {
        dao.setDefault(id)
    }

    override suspend fun keyHistoryForSupplier(supplierId: String): List<String> = withContext(ioDispatcher) {
        dao.getAll()
            .filter { it.supplierId == supplierId && it.apiKeyEncrypted.isNotEmpty() }
            .mapNotNull { entity ->
                runCatching { encryptor.decrypt(entity.id) }.getOrNull()?.takeIf { it.isNotBlank() }
            }
            .distinct()
    }

    override suspend fun urlHistory(supplierId: String?): List<String> = withContext(ioDispatcher) {
        val all = dao.getAll()
        val filtered = if (supplierId == null) all else all.filter { it.supplierId == supplierId }
        filtered.map { it.baseUrl }.filter { it.isNotBlank() }.distinct()
    }

    private fun ApiProfileEntity.toDomain(): ApiProfile = ApiProfile(
        id = id,
        title = title,
        supplierId = supplierId,
        baseUrl = baseUrl,
        modelName = modelName,
        isDefault = isDefault,
        hasApiKey = apiKeyEncrypted.isNotEmpty(),
        createdAt = createdAt,
        updatedAt = updatedAt
    )
}
