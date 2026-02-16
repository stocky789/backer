package com.backer.android.receiver

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import com.backer.android.worker.HeartbeatWorker
import java.util.concurrent.TimeUnit

/**
 * Receiver that starts the heartbeat worker on device boot.
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) {
            return
        }

        Log.d(TAG, "Boot completed, scheduling heartbeat worker")
        scheduleHeartbeat(context)
    }

    private fun scheduleHeartbeat(context: Context) {
        val constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()

        // Schedule periodic heartbeat every 15 minutes (minimum WorkManager interval)
        // The actual heartbeat uses long-polling for instant command delivery
        val heartbeatRequest = PeriodicWorkRequestBuilder<HeartbeatWorker>(
            15, TimeUnit.MINUTES
        )
            .setConstraints(constraints)
            .build()

        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            HeartbeatWorker.WORK_NAME,
            ExistingPeriodicWorkPolicy.KEEP,
            heartbeatRequest
        )

        Log.d(TAG, "Heartbeat worker scheduled")
    }

    companion object {
        private const val TAG = "BootReceiver"
    }
}