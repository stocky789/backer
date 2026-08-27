package com.backer.android.di

import com.backer.android.data.api.BackerApiService
import com.jakewharton.retrofit2.converter.kotlinx.serialization.asConverterFactory
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import kotlinx.serialization.json.Json
import okhttp3.Credentials
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import java.util.concurrent.TimeUnit
import javax.inject.Singleton

/**
 * Module for creating API service instances with dynamic base URL.
 * This is used when we need to create an API service with a specific server URL
 * before the user has registered (e.g., for testing connection).
 */
@Module
@InstallIn(SingletonComponent::class)
object NetworkModule {

    /**
     * Factory for creating BackerApiService instances with a specific base URL.
     */
    @Provides
    @Singleton
    fun provideApiServiceFactory(
        json: Json
    ): ApiServiceFactory {
        return ApiServiceFactory(json)
    }
}

/**
 * Factory for creating API service instances with dynamic base URLs.
 */
class ApiServiceFactory(private val json: Json) {

    /**
     * Create an API service for a specific server URL.
     * Used for testing connection before registration.
     */
    fun create(baseUrl: String): BackerApiService {
        // Use HEADERS level to avoid breaking @Streaming responses
        // BODY level buffers entire response, breaking streaming for large files
        val loggingInterceptor = HttpLoggingInterceptor().apply {
            level = HttpLoggingInterceptor.Level.HEADERS
        }

        val client = OkHttpClient.Builder()
            .addInterceptor(loggingInterceptor)
            .readTimeout(35, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .connectTimeout(15, TimeUnit.SECONDS)
            .build()

        val retrofit = Retrofit.Builder()
            .baseUrl(baseUrl.trimEnd('/') + "/")
            .client(client)
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()

        return retrofit.create(BackerApiService::class.java)
    }

    /**
     * Create an API service optimized for file transfers (backup/restore).
     * Uses longer timeouts suitable for large file uploads/downloads.
     */
    fun createForFileTransfer(
        baseUrl: String,
        clientId: String,
        clientSecret: String
    ): BackerApiService {
        // Use BASIC level for file transfers - only logs request/response lines
        // HEADERS or BODY would be too verbose for large transfers
        val loggingInterceptor = HttpLoggingInterceptor().apply {
            level = HttpLoggingInterceptor.Level.BASIC
        }

        val client = OkHttpClient.Builder()
            .addInterceptor { chain ->
                val request = chain.request()

                val authenticatedRequest = request.newBuilder()
                    .header("Authorization", Credentials.basic(clientId, clientSecret))
                    .build()
                chain.proceed(authenticatedRequest)
            }
            .addInterceptor(loggingInterceptor)
            // Longer timeouts for large file transfers
            // 10 minutes should handle most backup sizes over slow connections
            .readTimeout(10, TimeUnit.MINUTES)
            .writeTimeout(10, TimeUnit.MINUTES)
            .connectTimeout(30, TimeUnit.SECONDS)
            // Don't retry on connection failure during file transfer
            .retryOnConnectionFailure(false)
            .build()

        val retrofit = Retrofit.Builder()
            .baseUrl(baseUrl.trimEnd('/') + "/")
            .client(client)
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()

        return retrofit.create(BackerApiService::class.java)
    }
}
