package com.example.aichat.data.repository

import com.example.aichat.data.local.dao.FavoriteToolDao
import com.example.aichat.data.local.entity.FavoriteToolEntity
import com.example.aichat.di.IoDispatcher
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.withContext
import java.util.UUID
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Domain view of a [FavoriteToolEntity]. UI layers consume this so the raw
 * source string never crosses into Compose without an explicit type label.
 */
data class FavoriteTool(
    val id: String,
    val title: String,
    val content: String,
    val type: FavoriteToolType,
    val sourceMessageId: String?,
    val createdAt: Long
)

enum class FavoriteToolType { HTML, MARKDOWN }

@Singleton
class FavoriteToolRepository @Inject constructor(
    private val dao: FavoriteToolDao,
    @IoDispatcher private val ioDispatcher: CoroutineDispatcher
) {

    fun observeAll(): Flow<List<FavoriteTool>> =
        dao.observeAll().map { list -> list.map { it.toDomain() } }

    suspend fun getAll(): List<FavoriteTool> = withContext(ioDispatcher) {
        dao.getAll().map { it.toDomain() }
    }

    suspend fun getById(id: String): FavoriteTool? = withContext(ioDispatcher) {
        dao.getById(id)?.toDomain()
    }

    /**
     * Inserts a new tool. Generates id + createdAt. Returns the id so callers
     * can navigate / highlight the new entry.
     */
    suspend fun insert(
        title: String,
        content: String,
        type: FavoriteToolType,
        sourceMessageId: String? = null
    ): String = withContext(ioDispatcher) {
        val id = UUID.randomUUID().toString()
        dao.upsert(
            FavoriteToolEntity(
                id = id,
                title = title,
                content = content,
                type = when (type) {
                    FavoriteToolType.HTML -> FavoriteToolEntity.TYPE_HTML
                    FavoriteToolType.MARKDOWN -> FavoriteToolEntity.TYPE_MARKDOWN
                },
                sourceMessageId = sourceMessageId,
                createdAt = System.currentTimeMillis()
            )
        )
        id
    }

    suspend fun delete(id: String) = withContext(ioDispatcher) {
        dao.deleteById(id)
    }

    suspend fun count(): Int = withContext(ioDispatcher) { dao.count() }

    private fun FavoriteToolEntity.toDomain(): FavoriteTool = FavoriteTool(
        id = id,
        title = title,
        content = content,
        type = when (type) {
            FavoriteToolEntity.TYPE_HTML -> FavoriteToolType.HTML
            FavoriteToolEntity.TYPE_MARKDOWN -> FavoriteToolType.MARKDOWN
            else -> FavoriteToolType.MARKDOWN // forward-compat: unknown → markdown
        },
        sourceMessageId = sourceMessageId,
        createdAt = createdAt
    )
}
