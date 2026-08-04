package com.example.aichat.di

import android.content.Context
import com.example.aichat.data.remote.DataCollectionApi
import com.google.gson.Gson
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit
import javax.inject.Singleton

/**
 * DI module for the data collection (feedback, sessions, profiles) and
 * proactive agent communication layer.
 *
 * Provides a Retrofit instance pointing to the AiChat agent server.
 * The server URL is dynamic (resolved at call time via @Url), so this
 * Retrofit's baseUrl is just a placeholder — actual URLs are passed per-call.
 */
@Module
@InstallIn(SingletonComponent::class)
object DataCollectionModule {

    @Provides
    @Singleton
    fun provideDataCollectionOkHttp(): OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)   // long-poll for proactive messages
        .writeTimeout(30, TimeUnit.SECONDS)
        .build()

    @Provides
    @Singleton
    fun provideDataCollectionApi(okHttp: OkHttpClient): DataCollectionApi {
        return Retrofit.Builder()
            .baseUrl("http://localhost/") // placeholder — actual URL passed per-call via @Url
            .client(okHttp)
            .addConverterFactory(GsonConverterFactory.create(Gson()))
            .build()
            .create(DataCollectionApi::class.java)
    }
}
