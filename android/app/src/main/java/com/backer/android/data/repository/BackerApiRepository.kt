package com.backer.android.data.repository

import android.os.Build
import android.util.Log
import com.backer.android.BuildConfig
import com.backer.android.data.api.BackerApiService
import com.backer.android.data.api.models.BackupResult
import com.backer.android.data.api.models.BrowseResults
import com.backer.android.data.api.models.RestoreResult
import com.backer.android.data.api.models.HeartbeatRequest
import com.backer.android.data.api.models.HeartbeatResponse
import com.backer.android.data.api.models.ProgressReport
import com.backer.android.data.api.models.RegisterRequest
import com.backer.android.domain.model.ConnectionResult
import com.backer.android.domain.model.RegistrationResult
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Repository for Backer API operations.
 */
@Singleton
class BackerApiRepository @Inject constructor(
    private val apiService: BackerApiService,
    private val credentialRepository: CredentialRepository
) {
    /**
     * Test connection to the server.
     */
    suspend fun testConnection(): ConnectionResult = withContext(Dispatchers.IO) {
        try {
            val response = apiService.healthCheck()
            if (response.status == "ok") {
                ConnectionResult.Success(response.version ?: "unknown")
            } else {
                ConnectionResult.Error("Unexpected server response: ${response.status}")
            }
        } catch (e: Exception) {
            ConnectionResult.Error(e.message ?: "Connection failed")
        }
    }

    /**
     * Register this device as an agent with the server.
     */
    suspend fun register(): RegistrationResult = withContext(Dispatchers.IO) {
        try {
            val request = RegisterRequest(
                hostname = Build.MODEL,
                version = BuildConfig.VERSION_NAME,
                osInfo = "Android ${Build.VERSION.RELEASE}",
                tags = listOf("android")
            )

            val response = apiService.register(request)

            // Save credentials
            val serverUrl = credentialRepository.getServerUrl()
                ?: return@withContext RegistrationResult.Error("Server URL not set")

            credentialRepository.saveCredentials(
                serverUrl = serverUrl,
                clientId = response.clientId,
                clientSecret = response.clientSecret
            )

            RegistrationResult.Success(
                clientId = response.clientId,
                serverVersion = response.serverVersion
            )
        } catch (e: Exception) {
            RegistrationResult.Error(e.message ?: "Registration failed")
        }
    }

    /**
     * Send heartbeat to the server.
     * Returns pending commands if any.
     */
    suspend fun sendHeartbeat(): Result<HeartbeatResponse> = withContext(Dispatchers.IO) {
        try {
            val clientId = credentialRepository.getClientId()
                ?: return@withContext Result.failure(Exception("Not registered"))

            val response = apiService.heartbeat(HeartbeatRequest(clientId = clientId))
            credentialRepository.updateLastHeartbeat(System.currentTimeMillis())

            Result.success(response)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /**
     * Acknowledge a command was received.
     */
    suspend fun acknowledgeCommand(commandId: Int): Result<Unit> = withContext(Dispatchers.IO) {
        try {
            apiService.acknowledgeCommand(commandId)
            Result.success(Unit)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /**
     * Report progress during backup.
     */
    suspend fun reportProgress(progress: ProgressReport): Result<Unit> = withContext(Dispatchers.IO) {
        try {
            apiService.reportProgress(progress)
            Result.success(Unit)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /**
     * Report final backup result.
     */
    suspend fun reportResult(result: BackupResult): Result<Unit> = withContext(Dispatchers.IO) {
        try {
            apiService.reportResults(result)
            if (result.success) {
                credentialRepository.updateLastBackup(System.currentTimeMillis())
            }
            Result.success(Unit)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /**
     * Report browse filesystem results.
     */
    suspend fun reportBrowseResults(requestId: String, results: BrowseResults): Result<Unit> =
        withContext(Dispatchers.IO) {
            try {
                Log.d(TAG, "[BROWSE] Sending results for requestId=$requestId, " +
                    "entries=${results.entries.size}, success=${results.success}")
                val response = apiService.reportBrowseResults(requestId, results)
                if (response.isSuccessful) {
                    Log.i(TAG, "[BROWSE] Results reported successfully: ${response.code()}")
                    Result.success(Unit)
                } else {
                    val errorBody = response.errorBody()?.string() ?: "Unknown error"
                    Log.e(TAG, "[BROWSE] Server error: ${response.code()} - $errorBody")
                    Result.failure(Exception("Server error ${response.code()}: $errorBody"))
                }
            } catch (e: Exception) {
                Log.e(TAG, "[BROWSE] Exception reporting results: ${e.message}", e)
                Result.failure(e)
            }
        }

    /**
     * Report final restore result.
     */
    suspend fun reportRestoreResult(result: RestoreResult): Result<Unit> = withContext(Dispatchers.IO) {
        try {
            apiService.reportRestoreResults(result)
            Result.success(Unit)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    companion object {
        private const val TAG = "BackerApiRepository"
    }
}