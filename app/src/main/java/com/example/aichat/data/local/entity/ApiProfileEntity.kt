package com.example.aichat.data.local.entity

import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey

/**
 * Persistent LLM API configuration. Multiple profiles are allowed; exactly one is
 * active (isDefault = true) at a time. Created/edited in Settings → API Config.
 *
 * apiKeyEncrypted holds ciphertext produced by [com.example.aichat.data.security.ApiKeyEncryptor].
 */
@Entity(
    tableName = "api_profiles",
    indices = [Index(value = ["title"], unique = true)]
)
data class ApiProfileEntity(
    @PrimaryKey val id: String,
    val title: String,
    val supplierId: String,
    val baseUrl: String,
    val apiKeyEncrypted: String,
    val modelName: String,
    val isDefault: Boolean = false,
    val customFieldsJson: String = "{}",
    val createdAt: Long,
    val updatedAt: Long
)
