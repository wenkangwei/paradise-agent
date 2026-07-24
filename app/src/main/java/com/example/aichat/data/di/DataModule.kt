package com.example.aichat.data.di

import dagger.Module
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent

/**
 * Data layer DI module.
 *
 * Network-related providers (OkHttpClient, Retrofit, AiApiService, AiStreamClient, ConfigManager)
 * are provided by [NetworkModule].
 *
 * Repository bindings are provided by [com.example.aichat.di.RepositoryModule].
 *
 * Room database providers are in [com.example.aichat.di.DatabaseModule].
 */
@Module
@InstallIn(SingletonComponent::class)
object DataModule
