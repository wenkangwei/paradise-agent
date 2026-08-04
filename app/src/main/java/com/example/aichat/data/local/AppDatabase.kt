package com.example.aichat.data.local

import androidx.room.Database
import androidx.room.RoomDatabase
import androidx.room.TypeConverters
import com.example.aichat.data.local.dao.AgentProfileDao
import com.example.aichat.data.local.dao.ApiProfileDao
import com.example.aichat.data.local.dao.ConversationDao
import com.example.aichat.data.local.dao.FavoriteToolDao
import com.example.aichat.data.local.dao.MessageDao
import com.example.aichat.data.local.entity.AgentProfileEntity
import com.example.aichat.data.local.entity.ApiProfileEntity
import com.example.aichat.data.local.entity.ConversationEntity
import com.example.aichat.data.local.entity.FavoriteToolEntity
import com.example.aichat.data.local.entity.MessageEntity

@Database(
    entities = [
        ConversationEntity::class,
        MessageEntity::class,
        ApiProfileEntity::class,
        AgentProfileEntity::class,
        FavoriteToolEntity::class
    ],
    version = 10,
    exportSchema = false
)
@TypeConverters(AttachmentConverter::class)
abstract class AppDatabase : RoomDatabase() {
    abstract fun conversationDao(): ConversationDao
    abstract fun messageDao(): MessageDao
    abstract fun apiProfileDao(): ApiProfileDao
    abstract fun agentProfileDao(): AgentProfileDao
    abstract fun favoriteToolDao(): FavoriteToolDao
}
