package com.backer.android.data.api

import com.backer.android.data.api.models.AckResponse
import com.backer.android.data.api.models.BackupResult
import com.backer.android.data.api.models.BrowseResults
import com.backer.android.data.api.models.RestoreResult
import com.backer.android.data.api.models.HealthResponse
import com.backer.android.data.api.models.HeartbeatRequest
import com.backer.android.data.api.models.HeartbeatResponse
import com.backer.android.data.api.models.ProgressReport
import com.backer.android.data.api.models.RegisterRequest
import com.backer.android.data.api.models.RegisterResponse
import okhttp3.RequestBody
import okhttp3.ResponseBody
import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.Header
import retrofit2.http.POST
import retrofit2.http.Path
import retrofit2.http.Query
import retrofit2.http.Streaming

/**
 * Retrofit interface for the Backer server API.
 */
interface BackerApiService {

    /**
     * Health check endpoint.
     */
    @GET("/health")
    suspend fun healthCheck(): HealthResponse

    /**
     * Register a new agent with the server.
     * No authentication required for registration.
     */
    @POST("/api/v1/clients/register")
    suspend fun register(@Body request: RegisterRequest): RegisterResponse

    /**
     * Send heartbeat and receive pending commands.
     * Uses long-polling: server waits up to 25 seconds for commands.
     * Requires Basic Auth.
     */
    @POST("/api/v1/clients/heartbeat")
    suspend fun heartbeat(@Body request: HeartbeatRequest): HeartbeatResponse

    /**
     * Acknowledge that a command was received.
     * Requires Basic Auth.
     */
    @POST("/api/v1/commands/{commandId}/ack")
    suspend fun acknowledgeCommand(@Path("commandId") commandId: Int): AckResponse

    /**
     * Report progress during backup execution.
     * Requires Basic Auth.
     */
    @POST("/api/v1/progress")
    suspend fun reportProgress(@Body progress: ProgressReport)

    /**
     * Report final backup result.
     * Requires Basic Auth.
     */
    @POST("/api/v1/results")
    suspend fun reportResults(@Body result: BackupResult)

    /**
     * Report final restore result.
     * Requires Basic Auth.
     */
    @POST("/api/v1/results")
    suspend fun reportRestoreResults(@Body result: RestoreResult)

    /**
     * Report browse filesystem results.
     * Requires Basic Auth.
     */
    @POST("/api/v1/browse/{requestId}/results")
    suspend fun reportBrowseResults(
        @Path("requestId") requestId: String,
        @Body results: BrowseResults
    )

    /**
     * Upload backup data to the server (proxy backend).
     * Requires Basic Auth.
     *
     * The endpoint is /api/repo/{repoId}/backup where repoId comes from the destination path.
     * Headers:
     * - X-Backup-Subfolder: e.g., "Agents/jobname"
     * - X-Source-Path: original source path being backed up
     */
    @POST("/api/repo/{repoId}/backup")
    @Streaming
    suspend fun uploadBackup(
        @Path("repoId") repoId: String,
        @Header("X-Backup-Subfolder") subfolder: String,
        @Header("X-Source-Path") sourcePath: String,
        @Body body: RequestBody
    ): Response<ResponseBody>

    /**
     * Download restore data from the server (proxy backend).
     * Requires Basic Auth.
     *
     * The endpoint is /api/repo/{repoId}/restore
     * Headers:
     * - X-Restore-Subfolder: e.g., "Agents/jobname"
     * Query parameters:
     * - snapshot: snapshot identifier (optional, defaults to latest)
     */
    @GET("/api/repo/{repoId}/restore")
    @Streaming
    suspend fun downloadRestore(
        @Path("repoId") repoId: String,
        @Header("X-Restore-Subfolder") subfolder: String,
        @Query("snapshot") snapshot: String? = null
    ): Response<ResponseBody>
}