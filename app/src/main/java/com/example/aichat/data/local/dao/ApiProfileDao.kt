package com.example.aichat.data.local.dao

import androidx.room.Dao
import androidx.room.Delete
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import com.example.aichat.data.local.entity.ApiProfileEntity
import kotlinx.coroutines.flow.Flow

@Dao
interface ApiProfileDao {

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(profile: ApiProfileEntity)

    @Delete
    suspend fun delete(profile: ApiProfileEntity)

    @Query("DELETE FROM api_profiles WHERE id = :id")
    suspend fun deleteById(id: String)

    @Query("SELECT * FROM api_profiles ORDER BY createdAt ASC")
    fun observeAll(): Flow<List<ApiProfileEntity>>

    @Query("SELECT * FROM api_profiles ORDER BY createdAt ASC")
    suspend fun getAll(): List<ApiProfileEntity>

    @Query("SELECT * FROM api_profiles WHERE id = :id")
    suspend fun getById(id: String): ApiProfileEntity?

    @Query("SELECT * FROM api_profiles WHERE isDefault = 1 LIMIT 1")
    suspend fun getDefault(): ApiProfileEntity?

    @Query("SELECT * FROM api_profiles WHERE isDefault = 1 LIMIT 1")
    fun observeDefault(): Flow<ApiProfileEntity?>

    /**
     * Atomically sets isDefault=false on every row except the one with [id],
     * which is set to isDefault=true. Used when switching the active profile.
     *
     * Note: this works around Room not supporting CASE expressions in @Query updates
     * by relying on SQLite's boolean comparison (id = :id evaluates to 0 or 1).
     */
    @Query("UPDATE api_profiles SET isDefault = (id = :id)")
    suspend fun setDefault(id: String)
}
