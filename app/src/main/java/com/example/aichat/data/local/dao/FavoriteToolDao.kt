package com.example.aichat.data.local.dao

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import com.example.aichat.data.local.entity.FavoriteToolEntity
import kotlinx.coroutines.flow.Flow

@Dao
interface FavoriteToolDao {

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(tool: FavoriteToolEntity)

    @Query("DELETE FROM favorite_tools WHERE id = :id")
    suspend fun deleteById(id: String)

    @Query("SELECT * FROM favorite_tools ORDER BY createdAt DESC")
    fun observeAll(): Flow<List<FavoriteToolEntity>>

    @Query("SELECT * FROM favorite_tools ORDER BY createdAt DESC")
    suspend fun getAll(): List<FavoriteToolEntity>

    @Query("SELECT * FROM favorite_tools WHERE id = :id")
    suspend fun getById(id: String): FavoriteToolEntity?

    @Query("SELECT COUNT(*) FROM favorite_tools")
    suspend fun count(): Int
}
