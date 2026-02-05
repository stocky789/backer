package com.backer.android.service

import android.app.Notification
import android.app.Service
import android.content.Intent
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.backer.android.BackerApplication
import com.backer.android.R

/**
 * Foreground service for running backup operations.
 * Required for long-running operations on Android 8+.
 */
class BackupForegroundService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val jobName = intent?.getStringExtra(EXTRA_JOB_NAME) ?: "Unknown"

        val notification = createNotification(jobName)
        startForeground(NOTIFICATION_ID, notification)

        // The actual backup work is done by BackupWorker
        // This service just keeps the app alive

        return START_NOT_STICKY
    }

    private fun createNotification(jobName: String): Notification {
        return NotificationCompat.Builder(this, BackerApplication.CHANNEL_BACKUP)
            .setContentTitle(getString(R.string.notification_backup_running))
            .setContentText("Backing up: $jobName")
            .setSmallIcon(android.R.drawable.ic_popup_sync)
            .setOngoing(true)
            .setProgress(0, 0, true)
            .build()
    }

    fun updateProgress(progress: Int, currentFile: String) {
        val notification = NotificationCompat.Builder(this, BackerApplication.CHANNEL_BACKUP)
            .setContentTitle(getString(R.string.notification_backup_running))
            .setContentText(currentFile)
            .setSmallIcon(android.R.drawable.ic_popup_sync)
            .setOngoing(true)
            .setProgress(100, progress, false)
            .build()

        startForeground(NOTIFICATION_ID, notification)
    }

    companion object {
        const val NOTIFICATION_ID = 1001
        const val EXTRA_JOB_NAME = "job_name"
    }
}