package com.backer.android.worker

import android.app.NotificationManager
import android.content.Context
import android.content.pm.ServiceInfo
import android.os.Build
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.hilt.work.HiltWorker
import androidx.work.CoroutineWorker
import androidx.work.ForegroundInfo
import androidx.work.WorkerParameters
import com.backer.android.BackerApplication
import com.backer.android.data.api.models.ProgressReport
import com.backer.android.data.api.models.RestoreResult
import com.backer.android.data.repository.BackerApiRepository
import com.backer.android.data.repository.CredentialRepository
import com.backer.android.di.ApiServiceFactory
import com.backer.android.util.TarArchiveExtractor
import dagger.assisted.Assisted
import dagger.assisted.AssistedInject
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.time.Instant
import java.time.format.DateTimeFormatter

/**
 * Worker that executes restore jobs.
 * Downloads tar.gz archive from the Backer server and extracts to destination.
 */
@HiltWorker
class RestoreWorker @AssistedInject constructor(
    @Assisted context: Context,
    @Assisted params: WorkerParameters,
    private val credentialRepository: CredentialRepository,
    private val apiRepository: BackerApiRepository,
    private val apiServiceFactory: ApiServiceFactory,
    private val tarArchiveExtractor: TarArchiveExtractor
) : CoroutineWorker(context, params) {

    private val notificationManager =
        context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val jobName = inputData.getString("job_name") ?: return@withContext Result.failure()
        val runId = inputData.getString("run_id") ?: return@withContext Result.failure()
        val sourcePath = inputData.getString("source_path") ?: return@withContext Result.failure()
        val destinationPath = inputData.getString("destination_path") ?: return@withContext Result.failure()
        val snapshot = inputData.getString("snapshot")
        val cleanRestore = inputData.getBoolean("clean_restore", false)
        val dryRun = inputData.getBoolean("dry_run", false)
        val commandId = inputData.getInt("command_id", 0)
        val proxyCapability = inputData.getString("proxy_capability")?.takeIf { it.isNotBlank() }

        val clientId = credentialRepository.getClientId() ?: return@withContext Result.failure()
        val credentials = credentialRepository.getCredentials() ?: return@withContext Result.failure()

        Log.d(TAG, "Starting restore: job=$jobName, source=$sourcePath, dest=$destinationPath")

        val startedAt = Instant.now()
        var success = false
        var bytesRestored = 0L
        var filesRestored = 0
        val errors = mutableListOf<String>()
        var output = ""

        try {
            requireNotNull(proxyCapability) { "Android proxy restore missing required proxy_capability" }

            // Show foreground notification
            setForeground(createForegroundInfo(jobName, 0))

            // Report starting
            reportProgress(runId, "running", 0, "Initializing restore...")

            // Parse proxy URI to get repo_id and subfolder
            // Format: proxy://server:port/repo/{repo_id}/Agents/{job_name}
            val (repoId, subfolder) = parseProxyUri(sourcePath)
                ?: throw IllegalArgumentException("Invalid source path format: $sourcePath")

            Log.d(TAG, "Parsed source: repoId=$repoId, subfolder=$subfolder")

            // Verify destination directory
            val destDir = File(destinationPath)
            if (!destDir.exists()) {
                destDir.mkdirs()
            }
            if (!destDir.canWrite()) {
                throw IllegalArgumentException("Cannot write to destination path: $destinationPath")
            }

            reportProgress(runId, "running", 10, "Downloading from server...")
            setForegroundAsync(createForegroundInfo(jobName, 10))

            // Create API service with longer timeouts for file transfer
            val apiService = apiServiceFactory.createForFileTransfer(
                credentials.serverUrl,
                credentials.clientId,
                credentials.clientSecret
            )

            // Download restore data from server
            val response = apiService.downloadRestore(
                repoId = repoId,
                subfolder = subfolder,
                capability = proxyCapability,
                snapshot = snapshot
            )

            if (!response.isSuccessful) {
                val errorBody = response.errorBody()?.string() ?: "Unknown error"
                throw RuntimeException("Download failed: ${response.code()} - $errorBody")
            }

            val responseBody = response.body()
                ?: throw RuntimeException("Empty response from server")

            reportProgress(runId, "running", 50, "Extracting files...")
            setForegroundAsync(createForegroundInfo(jobName, 50))

            // Extract the archive
            val extractResult = tarArchiveExtractor.extractArchive(
                inputStream = responseBody.byteStream(),
                destDir = destDir,
                cleanRestore = cleanRestore,
                dryRun = dryRun
            ) { filesExtracted, currentFile, bytesExtracted ->
                // Update progress (50% to 95% for extraction)
                val percent = 50 + (filesExtracted.coerceAtMost(100) * 45 / 100)
                setForegroundAsync(createForegroundInfo(jobName, percent))
                // Fire-and-forget progress update (can't use suspend in callback)
                CoroutineScope(Dispatchers.IO).launch {
                    reportProgress(runId, "running", percent, currentFile)
                }
            }

            if (!extractResult.success) {
                errors.addAll(extractResult.errors)
                throw RuntimeException("Extraction failed: ${extractResult.errors.firstOrNull()}")
            }

            filesRestored = extractResult.filesExtracted
            bytesRestored = extractResult.bytesExtracted

            Log.d(TAG, "Restore completed: ${extractResult.filesExtracted} files, ${extractResult.bytesExtracted} bytes")

            success = true
            output = if (dryRun) {
                "Dry run completed: would restore $filesRestored files"
            } else {
                "Restore completed successfully: $filesRestored files restored"
            }

            reportProgress(runId, "finishing", 95, "Finalizing...")

        } catch (e: Exception) {
            Log.e(TAG, "Restore failed", e)
            errors.add(e.message ?: "Restore failed")
            output = e.stackTraceToString().take(5000)

            reportProgress(runId, "failed", 0, e.message ?: "Restore failed")
        }

        // Report final result
        val finishedAt = Instant.now()
        val result = RestoreResult(
            runId = runId,
            jobName = jobName,
            clientId = clientId,
            success = success,
            startedAt = DateTimeFormatter.ISO_INSTANT.format(startedAt),
            finishedAt = DateTimeFormatter.ISO_INSTANT.format(finishedAt),
            bytesRestored = bytesRestored,
            filesRestored = filesRestored,
            errors = errors,
            output = output.take(5000)
        )

        apiRepository.reportRestoreResult(result)
        if (commandId != 0) apiRepository.acknowledgeCommand(commandId)

        // Update notification
        showCompletionNotification(jobName, success)

        if (success) Result.success() else Result.failure()
    }

    /**
     * Parse proxy URI to extract repository ID and subfolder.
     *
     * Format: proxy://server:port/repo/{repo_id}/Agents/{job_name}
     *     or: proxys://server:port/repo/{repo_id}/Agents/{job_name}
     *
     * Returns Pair(repoId, subfolder) or null if parsing fails.
     */
    private fun parseProxyUri(uri: String): Pair<String, String>? {
        return try {
            // Check if it's a proxy URI
            if (!uri.startsWith("proxy://") && !uri.startsWith("proxys://")) {
                Log.w(TAG, "Not a proxy URI: $uri")
                return null
            }

            // Find /repo/ in the path
            val repoIndex = uri.indexOf("/repo/")
            if (repoIndex == -1) {
                Log.w(TAG, "No /repo/ found in URI: $uri")
                return null
            }

            // Extract everything after /repo/
            val pathAfterRepo = uri.substring(repoIndex + 6) // Skip "/repo/"

            // Split by / to get repo_id and subfolder parts
            val parts = pathAfterRepo.split("/", limit = 2)
            if (parts.isEmpty()) {
                Log.w(TAG, "No repo ID found in URI: $uri")
                return null
            }

            val repoId = parts[0]
            val subfolder = if (parts.size > 1) parts[1] else ""

            Log.d(TAG, "Parsed proxy URI: repoId=$repoId, subfolder=$subfolder")
            Pair(repoId, subfolder)
        } catch (e: Exception) {
            Log.e(TAG, "Failed to parse proxy URI: $uri", e)
            null
        }
    }

    private suspend fun reportProgress(
        runId: String,
        status: String,
        percent: Int,
        message: String
    ) {
        try {
            apiRepository.reportProgress(
                ProgressReport(
                    runId = runId,
                    status = status,
                    progressPercent = percent,
                    message = message
                )
            )
        } catch (e: Exception) {
            Log.w(TAG, "Failed to report progress", e)
        }
    }

    private fun createForegroundInfo(jobName: String, progress: Int): ForegroundInfo {
        val notification = NotificationCompat.Builder(applicationContext, BackerApplication.CHANNEL_BACKUP)
            .setContentTitle("Restoring")
            .setContentText(jobName)
            .setSmallIcon(android.R.drawable.ic_popup_sync)
            .setOngoing(true)
            .setProgress(100, progress, progress == 0)
            .build()

        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ForegroundInfo(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
            )
        } else {
            ForegroundInfo(NOTIFICATION_ID, notification)
        }
    }

    private fun showCompletionNotification(jobName: String, success: Boolean) {
        val notification = NotificationCompat.Builder(applicationContext, BackerApplication.CHANNEL_BACKUP)
            .setContentTitle(if (success) "Restore complete" else "Restore failed")
            .setContentText(jobName)
            .setSmallIcon(
                if (success) android.R.drawable.ic_dialog_info
                else android.R.drawable.ic_dialog_alert
            )
            .setAutoCancel(true)
            .build()

        notificationManager.notify(COMPLETION_NOTIFICATION_ID, notification)
    }

    companion object {
        private const val TAG = "RestoreWorker"
        private const val NOTIFICATION_ID = 2001
        private const val COMPLETION_NOTIFICATION_ID = 2002
    }
}
