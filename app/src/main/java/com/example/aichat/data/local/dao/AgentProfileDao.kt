package com.example.aichat.data.local.dao

import androidx.room.Dao
import androidx.room.Delete
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import com.example.aichat.data.local.entity.AgentProfileEntity
import kotlinx.coroutines.flow.Flow

@Dao
interface AgentProfileDao {

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(profile: AgentProfileEntity)

    @Delete
    suspend fun delete(profile: AgentProfileEntity)

    @Query("DELETE FROM agent_profiles WHERE id = :id")
    suspend fun deleteById(id: String)

    @Query("SELECT * FROM agent_profiles ORDER BY createdAt ASC")
    fun observeAll(): Flow<List<AgentProfileEntity>>

    @Query("SELECT * FROM agent_profiles ORDER BY createdAt ASC")
    suspend fun getAll(): List<AgentProfileEntity>

    @Query("SELECT * FROM agent_profiles WHERE id = :id")
    suspend fun getById(id: String): AgentProfileEntity?

    @Query("SELECT * FROM agent_profiles WHERE isDefault = 1 LIMIT 1")
    suspend fun getDefault(): AgentProfileEntity?

    @Query("SELECT * FROM agent_profiles WHERE isDefault = 1 LIMIT 1")
    fun observeDefault(): Flow<AgentProfileEntity?>

    @Query("UPDATE agent_profiles SET isDefault = (id = :id)")
    suspend fun setDefault(id: String)
}
