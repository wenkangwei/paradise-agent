package com.example.aichat.data.local

import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

/**
 * v2 → v3: Add status / reasoningContent / metadataJson columns to messages.
 *
 * status stores MessageStatus.name (COMPLETE/INTERRUPTED/FAILED).
 * reasoningContent holds optional AI reasoning text (DeepSeek-R1, Claude thinking, etc.).
 * metadataJson holds a JSON-encoded MessageMetadata object.
 */
val MIGRATION_2_3 = object : Migration(2, 3) {
    override fun migrate(db: SupportSQLiteDatabase) {
        db.execSQL(
            "ALTER TABLE messages ADD COLUMN status TEXT NOT NULL DEFAULT 'COMPLETE'"
        )
        db.execSQL("ALTER TABLE messages ADD COLUMN reasoningContent TEXT")
        db.execSQL("ALTER TABLE messages ADD COLUMN metadataJson TEXT")
    }
}

/**
 * v3 → v4: Create api_profiles table for multi-provider configuration.
 *
 * Replaces the previous single-config SharedPreferences (ConfigManager) approach.
 * Migration from SharedPreferences to this table is handled at the repository layer
 * on first launch (see ApiProfileRepository.bootstrapIfNeeded).
 */
val MIGRATION_3_4 = object : Migration(3, 4) {
    override fun migrate(db: SupportSQLiteDatabase) {
        db.execSQL(
            """
            CREATE TABLE IF NOT EXISTS api_profiles (
                id TEXT NOT NULL PRIMARY KEY,
                title TEXT NOT NULL,
                supplierId TEXT NOT NULL,
                baseUrl TEXT NOT NULL,
                apiKeyEncrypted TEXT NOT NULL,
                modelName TEXT NOT NULL,
                isDefault INTEGER NOT NULL DEFAULT 0,
                customFieldsJson TEXT NOT NULL DEFAULT '{}',
                createdAt INTEGER NOT NULL,
                updatedAt INTEGER NOT NULL
            )
            """.trimIndent()
        )
        db.execSQL(
            "CREATE UNIQUE INDEX IF NOT EXISTS index_api_profiles_title ON api_profiles(title)"
        )
    }
}

/**
 * v4 → v5: Create agent_profiles table for Agent persona configuration.
 */
val MIGRATION_4_5 = object : Migration(4, 5) {
    override fun migrate(db: SupportSQLiteDatabase) {
        db.execSQL(
            """
            CREATE TABLE IF NOT EXISTS agent_profiles (
                id TEXT NOT NULL PRIMARY KEY,
                name TEXT NOT NULL,
                systemPrompt TEXT NOT NULL,
                avatar TEXT,
                boundApiProfileId TEXT,
                capabilitiesJson TEXT NOT NULL DEFAULT '[]',
                temperature REAL,
                maxTokens INTEGER,
                isDefault INTEGER NOT NULL DEFAULT 0,
                createdAt INTEGER NOT NULL,
                updatedAt INTEGER NOT NULL
            )
            """.trimIndent()
        )
        db.execSQL(
            "CREATE UNIQUE INDEX IF NOT EXISTS index_agent_profiles_name ON agent_profiles(name)"
        )
    }
}

/**
 * v5 → v6: Add `reaction` column to messages for AI message feedback
 * ("like" / "dislike" / null). User messages keep null.
 */
val MIGRATION_5_6 = object : Migration(5, 6) {
    override fun migrate(db: SupportSQLiteDatabase) {
        db.execSQL("ALTER TABLE messages ADD COLUMN reaction TEXT")
    }
}

/** All migrations from the initial v2 schema to the current v6. */
val ALL_MIGRATIONS = arrayOf(MIGRATION_2_3, MIGRATION_3_4, MIGRATION_4_5, MIGRATION_5_6)
