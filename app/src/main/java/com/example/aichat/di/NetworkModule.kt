package com.example.aichat.di

import android.content.Context
import com.example.aichat.data.provider.DefaultSupplierRegistry
import com.example.aichat.data.provider.LlmProviderFactory
import com.example.aichat.data.provider.LlmProviderFactoryImpl
import com.example.aichat.data.provider.SupplierRegistry
import com.example.aichat.data.remote.AiStreamClient
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import okhttp3.logging.HttpLoggingInterceptor
import javax.inject.Singleton

/**
 * Network-stack DI bindings.
 *
 * Design change vs. old code:
 *   Previously `provideRetrofit` was @Singleton with a single baseUrl captured at
 *   startup, which made switching API profiles require an app restart. That binding
 *   is GONE. The stack (OkHttp client builder + Retrofit) is now constructed
 *   per-profile inside [LlmProviderFactoryImpl] and cached by profile id.
 *
 * What this module provides instead:
 *   - [SupplierRegistry] (the catalog of built-in suppliers, injectable)
 *   - [LlmProviderFactory] (the per-profile stack builder)
 *   - A shared [HttpLoggingInterceptor] kept out of the factory so the log level
 *     can be tuned centrally without leaking prompts in release builds.
 */
@Module
@InstallIn(SingletonComponent::class)
object NetworkModule {

    @Provides
    @Singleton
    fun provideSupplierRegistry(): SupplierRegistry = DefaultSupplierRegistry()

    @Provides
    @Singleton
    fun provideLlmProviderFactory(
        registry: SupplierRegistry,
        streamClient: AiStreamClient
    ): LlmProviderFactory = LlmProviderFactoryImpl(registry, streamClient)

    @Provides
    @Singleton
    fun provideLoggingInterceptor(): HttpLoggingInterceptor =
        HttpLoggingInterceptor().apply {
            level = HttpLoggingInterceptor.Level.BASIC // avoid leaking prompts in logs
        }
}
