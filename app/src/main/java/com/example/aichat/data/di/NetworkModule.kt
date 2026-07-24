package com.example.aichat.data.di

import com.example.aichat.data.remote.AiApiService
import com.example.aichat.data.remote.AiStreamClient
import com.example.aichat.data.remote.ConfigManager
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit
import javax.inject.Singleton

@Module
@InstallIn(SingletonComponent::class)
object NetworkModule {

    // ConfigManager is provided via @Inject constructor — no @Provides needed.

    @Provides
    @Singleton
    fun provideLoggingInterceptor(): HttpLoggingInterceptor =
        HttpLoggingInterceptor().apply {
            level = HttpLoggingInterceptor.Level.HEADERS
        }

    @Provides
    @Singleton
    fun provideAuthInterceptor(configManager: ConfigManager): Interceptor =
        Interceptor { chain ->
            val config = configManager.currentConfig()
            val request = chain.request().newBuilder().apply {
                addHeader("Authorization", "Bearer ${config.apiKey}")
                addHeader("Content-Type", "application/json")
            }.build()
            chain.proceed(request)
        }

    @Provides
    @Singleton
    fun provideOkHttpClient(
        loggingInterceptor: HttpLoggingInterceptor,
        authInterceptor: Interceptor
    ): OkHttpClient = OkHttpClient.Builder()
        .addInterceptor(authInterceptor)
        .addInterceptor(loggingInterceptor)
        .connectTimeout(60, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .callTimeout(120, TimeUnit.SECONDS)
        .build()

    @Provides
    @Singleton
    fun provideRetrofit(
        client: OkHttpClient,
        configManager: ConfigManager
    ): Retrofit {
        val baseUrl = configManager.currentConfig().baseUrl.let {
            if (it.endsWith("/")) it else "$it/"
        }
        return Retrofit.Builder()
            .baseUrl(baseUrl)
            .client(client)
            .addConverterFactory(GsonConverterFactory.create())
            .build()
    }

    @Provides
    @Singleton
    fun provideAiApiService(retrofit: Retrofit): AiApiService =
        retrofit.create(AiApiService::class.java)

    @Provides
    @Singleton
    fun provideAiStreamClient(): AiStreamClient = AiStreamClient()
}
