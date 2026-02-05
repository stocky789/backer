package com.backer.android.worker

import android.content.Context
import android.util.Log
import androidx.hilt.work.HiltWorker
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.backer.android.data.repository.BackerApiRepository
import com.backer.android.data.repository.CredentialRepository
import dagger.assisted.Assisted
import dagger.assisted.AssistedInject
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Background worker that sends heartbeats to the Backer server.
 * Uses long-polling: the server holds the request for up to 25 seconds
 * waiting for commands, so we get near-instant command delivery.
 */
@HiltWorker
class HeartbeatWorker @AssistedInject constructor(
    @Assisted context: Context,
    @Assisted params: WorkerParameters,
    private val apiRepository: BackerApiRepository,
    private val credentialRepository: CredentialRepository,
    private val commandHandler: CommandHandler
) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        if (!credentialRepository.isRegistered()) {
            Log.w(TAG, "Not registered, skipping heartbeat")
            return@withContext Result.failure()
        }

        try {
            val response = apiRepository.sendHeartbeat()

            response.fold(
                onSuccess = { heartbeatResponse ->
                    Log.d(TAG, "Heartbeat successful, ${heartbeatResponse.commands.size} commands received")

                    // Process any pending commands
                    heartbeatResponse.commands.forEach { command ->
                        Log.d(TAG, "Received command: ${command.commandType} (id=${command.id})")

                        // Acknowledge the command
                        apiRepository.acknowledgeCommand(command.id)

                        // Handle the command via CommandHandler
                        commandHandler.handle(command)
                    }

                    Result.success()
                },
                onFailure = { error ->
                    Log.e(TAG, "Heartbeat failed", error)
                    if (runAttemptCount < MAX_RETRIES) {
                        Result.retry()
                    } else {
                        Result.failure()
                    }
                }
            )
        } catch (e: Exception) {
            Log.e(TAG, "Heartbeat exception", e)
            if (runAttemptCount < MAX_RETRIES) {
                Result.retry()
            } else {
                Result.failure()
            }
        }
    }

    companion object {
        private const val TAG = "HeartbeatWorker"
        private const val MAX_RETRIES = 3
        const val WORK_NAME = "heartbeat_work"
    }
}