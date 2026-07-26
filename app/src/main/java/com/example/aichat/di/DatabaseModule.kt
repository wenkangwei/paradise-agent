package com.example.aichat.di

import android.content.Context
import androidx.room.Room
import com.example.aichat.data.local.ALL_MIGRATIONS
import com.example.aichat.data.local.AppDatabase
import com.example.aichat.data.local.dao.AgentProfileDao
import com.example.aichat.data.local.dao.ApiProfileDao
import com.example.aichat.data.local.dao.ConversationDao
import com.example.aichat.data.local.dao.FavoriteToolDao
import com.example.aichat.data.local.dao.MessageDao
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

@Module
@InstallIn(SingletonComponent::class)
object DatabaseModule {

    @Provides
    @Singleton
    fun provideAppDatabase(
        @ApplicationContext context: Context
    ): AppDatabase = Room.databaseBuilder(
        context,
        AppDatabase::class.java,
        "aichat.db"
    )
        .addMigrations(*ALL_MIGRATIONS)
        .enableMultiInstanceInvalidation()
        .build()

    @Provides
    fun provideConversationDao(db: AppDatabase): ConversationDao = db.conversationDao()

    @Provides
    fun provideMessageDao(db: AppDatabase): MessageDao = db.messageDao()

    @Provides
    fun provideApiProfileDao(db: AppDatabase): ApiProfileDao = db.apiProfileDao()

    @Provides
    fun provideAgentProfileDao(db: AppDatabase): AgentProfileDao = db.agentProfileDao()

    @Provides
    fun provideFavoriteToolDao(db: AppDatabase): FavoriteToolDao = db.favoriteToolDao()
}
