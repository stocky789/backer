package com.backer.android.worker

import android.content.Context
import android.util.Log
import androidx.hilt.work.HiltWorker
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.backer.android.data.repository.BackerApiRepository
import com.backer.android.data.repository.CredentialRepository
import dagger.assisted.Assisted
import dagger.assisted.AssistedInject
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.concurrent.TimeUnit

/**
 * Background worker that sends heartbeats to the Backer server.
 * Uses long-polling: the server holds the request for up to 25 seconds
 * waiting for commands, so we get near-instant command delivery.
 *
 * After each successful heartbeat, schedules another immediate heartbeat
 * to maintain continuous command delivery.
 */
@HiltWorker
class HeartbeatWorker @AssistedInject constructor(
    @Assisted private val context: Context,
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
            Log.d(TAG, "Sending heartbeat...")
            val response = apiRepository.sendHeartbeat()

            response.fold(
                onSuccess = { heartbeatResponse ->
                    Log.d(TAG, "Heartbeat successful, ${heartbeatResponse.commands.size} commands received")

                    // Process any pending commands
                    heartbeatResponse.commands.forEach { command ->
                        Log.i(TAG, "Received command: ${command.commandType} (id=${command.id})")

                        // Handle the command via CommandHandler
                        commandHandler.handle(command)
                        if (command.commandType !in setOf("backup", "restore")) {
                            apiRepository.acknowledgeCommand(command.id)
                        }
                    }

                    // Schedule next heartbeat immediately to maintain continuous polling
                    scheduleNextHeartbeat()

                    Result.success()
                },
                onFailure = { error ->
                    Log.e(TAG, "Heartbeat failed: ${error.message}", error)

                    // Still schedule next heartbeat even on failure (with delay)
                    scheduleNextHeartbeat(delaySeconds = 30)

                    if (runAttemptCount < MAX_RETRIES) {
                        Result.retry()
                    } else {
                        Result.failure()
                    }
                }
            )
        } catch (e: Exception) {
            Log.e(TAG, "Heartbeat exception: ${e.message}", e)

            // Still schedule next heartbeat even on exception (with delay)
            scheduleNextHeartbeat(delaySeconds = 30)

            if (runAttemptCount < MAX_RETRIES) {
                Result.retry()
            } else {
                Result.failure()
            }
        }
    }

    /**
     * Schedule the next heartbeat worker.
     * This creates a continuous loop of heartbeats while registered.
     */
    private fun scheduleNextHeartbeat(delaySeconds: Long = 0) {
        if (!credentialRepository.isRegistered()) {
            Log.d(TAG, "Not scheduling next heartbeat - not registered")
            return
        }

        val constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()

        val nextHeartbeat = OneTimeWorkRequestBuilder<HeartbeatWorker>()
            .setConstraints(constraints)
            .apply {
                if (delaySeconds > 0) {
                    setInitialDelay(delaySeconds, TimeUnit.SECONDS)
                }
            }
            .build()

        WorkManager.getInstance(context).enqueueUniqueWork(
            CONTINUOUS_HEARTBEAT_WORK,
            ExistingWorkPolicy.REPLACE,
            nextHeartbeat
        )

        Log.d(TAG, "Scheduled next heartbeat${if (delaySeconds > 0) " in ${delaySeconds}s" else " immediately"}")
    }

    companion object {
        private const val TAG = "HeartbeatWorker"
        private const val MAX_RETRIES = 3
        const val WORK_NAME = "heartbeat_work"
        private const val CONTINUOUS_HEARTBEAT_WORK = "continuous_heartbeat"
    }
}
