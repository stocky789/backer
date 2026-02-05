package com.backer.android.worker

import android.content.Context
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
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

    /**
     * Start the periodic heartbeat worker.
     * Uses 15-minute interval (minimum for WorkManager).
     * The actual heartbeat uses long-polling for instant command delivery.
     */
    fun startHeartbeat() {
        val constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()

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