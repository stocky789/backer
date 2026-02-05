package com.backer.android.worker

import android.content.Context
import android.util.Log
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import dagger.hilt.android.qualifiers.ApplicationContext
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Schedules background workers for heartbeat and backup operations.
 */
@Singleton
class WorkManagerScheduler @Inject constructor(
    @ApplicationContext private val context: Context
) {
    private val workManager = WorkManager.getInstance(context)

    companion object {
        private const val TAG = "WorkManagerScheduler"
        private const val IMMEDIATE_HEARTBEAT_WORK = "immediate_heartbeat"
    }

    /**
     * Start the periodic heartbeat worker.
     * Uses 15-minute interval (minimum for WorkManager).
     * The actual heartbeat uses long-polling for instant command delivery.
     *
     * Also triggers an immediate heartbeat so we don't wait 15 minutes for the first one.
     */
    fun startHeartbeat() {
        Log.i(TAG, "Starting heartbeat worker")

        val constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()

        // Schedule periodic heartbeat (15 min minimum for WorkManager)
        val heartbeatRequest = PeriodicWorkRequestBuilder<HeartbeatWorker>(
            15, TimeUnit.MINUTES,
            5, TimeUnit.MINUTES // Flex interval
        )
            .setConstraints(constraints)
            .build()

        workManager.enqueueUniquePeriodicWork(
            HeartbeatWorker.WORK_NAME,
            ExistingPeriodicWorkPolicy.UPDATE,
            heartbeatRequest
        )

        // Also trigger an immediate heartbeat so we don't wait 15 minutes
        triggerImmediateHeartbeat()
    }

    /**
     * Trigger an immediate one-time heartbeat.
     * Useful after registration or when we need to check for commands right away.
     */
    fun triggerImmediateHeartbeat() {
        Log.i(TAG, "Triggering immediate heartbeat")

        val constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()

        val immediateRequest = OneTimeWorkRequestBuilder<HeartbeatWorker>()
            .setConstraints(constraints)
            .build()

        workManager.enqueueUniqueWork(
            IMMEDIATE_HEARTBEAT_WORK,
            ExistingWorkPolicy.REPLACE,
            immediateRequest
        )
    }

    /**
     * Stop the heartbeat worker.
     */
    fun stopHeartbeat() {
        workManager.cancelUniqueWork(HeartbeatWorker.WORK_NAME)
    }

    /**
     * Cancel all scheduled work.
     */
    fun cancelAll() {
        workManager.cancelAllWork()
    }
}