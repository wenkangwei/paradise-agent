package com.example.aichat.data.local.entity

import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey

/**
 * Persistent Agent persona configuration. Multiple agents are allowed; exactly one
 * is active (isDefault = true). Created/edited in Settings → Agent Config.
 *
 * boundApiProfileId: when null the agent follows the global active ApiProfile;
 * otherwise it pins to the specified profile regardless of global active.
 */
@Entity(
    tableName = "agent_profiles",
    indices = [Index(value = ["name"], unique = true)]
)
data class AgentProfileEntity(
    @PrimaryKey val id: String,
    val name: String,
    val systemPrompt: String,
    val avatar: String? = null,
    val boundApiProfileId: String? = null,
    val capabilitiesJson: String = "[]",
    val temperature: Float? = null,
    val maxTokens: Int? = null,
    val isDefault: Boolean = false,
    val createdAt: Long,
    val updatedAt: Long
)
