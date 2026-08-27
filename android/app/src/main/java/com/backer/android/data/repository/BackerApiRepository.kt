package com.backer.android.data.repository

import android.util.Log
import com.backer.android.data.api.BackerApiService
import com.backer.android.data.api.models.BackupResult
import com.backer.android.data.api.models.BrowseResults
import com.backer.android.data.api.models.HeartbeatRequest
import com.backer.android.data.api.models.HeartbeatResponse
import com.backer.android.data.api.models.ProgressReport
import com.backer.android.data.api.models.RestoreResult
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
     * Send heartbeat to the server.
     * Returns pending commands if any.
     */
    suspend fun sendHeartbeat(): Result<HeartbeatResponse> = withContext(Dispatchers.IO) {
        runCatching {
            val clientId = credentialRepository.getClientId()
                ?: error("Not registered")

            val response = apiService.heartbeat(HeartbeatRequest(clientId = clientId))
            credentialRepository.updateLastHeartbeat(System.currentTimeMillis())
            response
        }
    }

    /**
     * Acknowledge a command was received.
     */
    suspend fun acknowledgeCommand(commandId: Int): Result<Unit> = withContext(Dispatchers.IO) {
        runCatching {
            apiService.acknowledgeCommand(commandId)
            Unit
        }
    }

    /**
     * Report progress during backup.
     */
    suspend fun reportProgress(progress: ProgressReport): Result<Unit> = withContext(Dispatchers.IO) {
        runCatching {
            apiService.reportProgress(progress)
        }
    }

    /**
     * Report final backup result.
     */
    suspend fun reportResult(result: BackupResult): Result<Unit> = withContext(Dispatchers.IO) {
        runCatching {
            apiService.reportResults(result)
            if (result.success) {
                credentialRepository.updateLastBackup(System.currentTimeMillis())
            }
        }
    }

    /**
     * Report browse filesystem results.
     */
    suspend fun reportBrowseResults(requestId: String, results: BrowseResults): Result<Unit> =
        withContext(Dispatchers.IO) {
            runCatching {
                Log.d(TAG, "[BROWSE] Sending results for requestId=$requestId, " +
                    "entries=${results.entries.size}, success=${results.success}")
                val response = apiService.reportBrowseResults(requestId, results)
                if (response.isSuccessful) {
                    Log.i(TAG, "[BROWSE] Results reported successfully: ${response.code()}")
                } else {
                    val errorBody = response.errorBody()?.string() ?: "Unknown error"
                    Log.e(TAG, "[BROWSE] Server error: ${response.code()} - $errorBody")
                    error("Server error ${response.code()}: $errorBody")
                }
                Unit
            }
        }

    /**
     * Report final restore result.
     */
    suspend fun reportRestoreResult(result: RestoreResult): Result<Unit> = withContext(Dispatchers.IO) {
        runCatching {
            apiService.reportRestoreResults(result)
        }
    }

    companion object {
        private const val TAG = "BackerApiRepository"
    }
}
