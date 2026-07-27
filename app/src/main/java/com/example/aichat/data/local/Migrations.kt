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

/**
 * v6 → v7: Add `fullUrlMode` column to api_profiles.
 *
 * When true, the stored baseUrl is treated as the *complete* chat-completions
 * endpoint and the request layer skips appending `/chat/completions`. Existing
 * rows default to false (legacy behaviour) so this is a non-breaking change.
 */
val MIGRATION_6_7 = object : Migration(6, 7) {
    override fun migrate(db: SupportSQLiteDatabase) {
        db.execSQL(
            "ALTER TABLE api_profiles ADD COLUMN fullUrlMode INTEGER NOT NULL DEFAULT 0"
        )
    }
}

/**
 * v7 → v8: Deduplicate legacy user messages.
 *
 * Before v3.5.4, `StreamAiReplyUseCase` re-inserted the user message with a
 * fresh UUID after `ChatViewModel` had already inserted it — every user turn
 * ended up duplicated in the database. The code bug was fixed in v3.5.4 but
 * existing rows were never cleaned up, so users on legacy DBs kept seeing
 * "message appears twice" when re-entering old sessions.
 *
 * This migration keeps the earliest row (MIN(id) lexicographically — UUIDs
 * are time-ordered so this approximates "first inserted") per
 * (conversationId, content, timestamp) tuple and deletes the rest.
 */
val MIGRATION_7_8 = object : Migration(7, 8) {
    override fun migrate(db: SupportSQLiteDatabase) {
        // Wrap in a transaction so a partial delete doesn't leave the DB in
        // an inconsistent state if the process dies mid-migration.
        db.beginTransaction()
        try {
            db.execSQL(
                """
                DELETE FROM messages
                WHERE id NOT IN (
                    SELECT MIN(id) FROM messages
                    GROUP BY conversationId, content, timestamp
                )
                """.trimIndent()
            )
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }
}

val MIGRATION_8_9 = object : Migration(8, 9) {
    override fun migrate(db: SupportSQLiteDatabase) {
        // Pin-able tool cards extracted from AI replies (HTML page or long
        // markdown document). See FavoriteToolEntity for the recognition rule.
        db.execSQL(
            """
            CREATE TABLE IF NOT EXISTS favorite_tools (
                id TEXT NOT NULL PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                type TEXT NOT NULL,
                sourceMessageId TEXT,
                createdAt INTEGER NOT NULL
            )
            """.trimIndent()
        )
        db.execSQL(
            "CREATE INDEX IF NOT EXISTS index_favorite_tools_createdAt ON favorite_tools(createdAt)"
        )
    }
}

/**
 * v9 → v10: add `messages.updatedAt` so the main process's
 * orphan-streaming watchdog can distinguish a TRULY orphaned row
 * (no writer for >2 min) from one that the :streaming process is
 * actively writing every 150ms. Without this column the watchdog
 * had to mark *every* STREAMING row as interrupted on init, which
 * raced with active streams after a lock-screen cycle and made
 * the AI bubble "disappear" (the user's recurring complaint).
 *
 * Legacy rows back-fill `updatedAt = timestamp` — they're all
 * finalised (COMPLETE/INTERRUPTED/FAILED) so the value is unused.
 */
val MIGRATION_9_10 = object : Migration(9, 10) {
    override fun migrate(db: SupportSQLiteDatabase) {
        db.execSQL(
            "ALTER TABLE messages ADD COLUMN updatedAt INTEGER NOT NULL DEFAULT 0"
        )
        // Back-fill from timestamp so legacy rows have a sensible value.
        db.execSQL(
            "UPDATE messages SET updatedAt = timestamp WHERE updatedAt = 0"
        )
        // Index to find orphaned STREAMING rows cheaply on watchdog tick.
        db.execSQL(
            "CREATE INDEX IF NOT EXISTS index_messages_status_updatedAt ON messages(status, updatedAt)"
        )
    }
}

/** All migrations from the initial v2 schema to the current v10. */
val ALL_MIGRATIONS = arrayOf(
    MIGRATION_2_3,
    MIGRATION_3_4,
    MIGRATION_4_5,
    MIGRATION_5_6,
    MIGRATION_6_7,
    MIGRATION_7_8,
    MIGRATION_8_9,
    MIGRATION_9_10
)
