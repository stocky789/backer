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
import com.backer.android.R
import com.backer.android.data.api.BackerApiService
import com.backer.android.data.api.models.BackupResult
import com.backer.android.data.api.models.ProgressReport
import com.backer.android.data.repository.BackerApiRepository
import com.backer.android.data.repository.CredentialRepository
import com.backer.android.di.ApiServiceFactory
import com.backer.android.util.TarArchiveCreator
import dagger.assisted.Assisted
import dagger.assisted.AssistedInject
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.asRequestBody
import java.io.File
import java.time.Instant
import java.time.format.DateTimeFormatter

/**
 * Worker that executes backup jobs.
 * Creates a tar.gz archive and streams it to the Backer server.
 */
@HiltWorker
class BackupWorker @AssistedInject constructor(
    @Assisted context: Context,
    @Assisted params: WorkerParameters,
    private val credentialRepository: CredentialRepository,
    private val apiRepository: BackerApiRepository,
    private val apiServiceFactory: ApiServiceFactory,
    private val tarArchiveCreator: TarArchiveCreator
) : CoroutineWorker(context, params) {

    private val notificationManager =
        context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val jobName = inputData.getString("job_name") ?: return@withContext Result.failure()
        val runId = inputData.getString("run_id") ?: return@withContext Result.failure()
        val sourcePath = inputData.getString("source_path") ?: return@withContext Result.failure()
        val destinationPath = inputData.getString("destination_path") ?: return@withContext Result.failure()
        val excludesJson = inputData.getString("excludes") ?: "[]"

        val clientId = credentialRepository.getClientId() ?: return@withContext Result.failure()
        val credentials = credentialRepository.getCredentials() ?: return@withContext Result.failure()

        Log.d(TAG, "Starting backup: job=$jobName, source=$sourcePath")

        val startedAt = Instant.now()
        var success = false
        var bytesTransferred = 0L
        var filesTransferred = 0
        val errors = mutableListOf<String>()
        var output = ""

        try {
            // Show foreground notification
            setForeground(createForegroundInfo(jobName, 0))

            // Report starting
            reportProgress(runId, "running", 0, "Initializing backup...")

            // Parse excludes
            val excludes = parseExcludes(excludesJson)

            // Verify source exists
            val sourceDir = File(sourcePath)
            if (!sourceDir.exists()) {
                throw IllegalArgumentException("Source path does not exist: $sourcePath")
            }
            if (!sourceDir.isDirectory) {
                throw IllegalArgumentException("Source path is not a directory: $sourcePath")
            }
            if (!sourceDir.canRead()) {
                throw IllegalArgumentException("Cannot read source path: $sourcePath")
            }

            reportProgress(runId, "running", 5, "Creating archive...")

            // Create temp file for archive
            val tempFile = File.createTempFile("backup_", ".tar.gz", applicationContext.cacheDir)

            try {
                // Create tar.gz archive
                val archiveResult = tarArchiveCreator.createArchive(
                    sourceDir = sourceDir,
                    outputFile = tempFile,
                    excludePatterns = excludes
                ) { filesProcessed, currentFile, bytesProcessed ->
                    // Update progress (5% to 70% for archive creation)
                    val percent = 5 + (filesProcessed.coerceAtMost(100) * 65 / 100)
                    setForegroundAsync(createForegroundInfo(jobName, percent))
                    reportProgress(runId, "running", percent, currentFile)
                }

                if (!archiveResult.success) {
                    errors.addAll(archiveResult.errors)
                    throw RuntimeException("Archive creation failed: ${archiveResult.errors.firstOrNull()}")
                }

                filesTransferred = archiveResult.filesProcessed
                bytesTransferred = archiveResult.bytesProcessed

                Log.d(TAG, "Archive created: ${archiveResult.filesProcessed} files, ${tempFile.length()} bytes")

                reportProgress(runId, "running", 75, "Uploading to server...")
                setForegroundAsync(createForegroundInfo(jobName, 75))

                // Parse proxy URI to get repo_id and subfolder
                // Format: proxy://server:port/repo/{repo_id}/Agents/{job_name}
                // or: proxys://server:port/repo/{repo_id}/Agents/{job_name}
                val (repoId, subfolder) = parseProxyUri(destinationPath)
                    ?: throw IllegalArgumentException("Invalid destination path format: $destinationPath")

                Log.d(TAG, "Parsed destination: repoId=$repoId, subfolder=$subfolder")

                // Upload to server (use file transfer client with longer timeouts)
                val apiService = apiServiceFactory.createForFileTransfer(
                    credentials.serverUrl,
                    credentials.clientId,
                    credentials.clientSecret
                )

                val requestBody = tempFile.asRequestBody("application/gzip".toMediaType())

                val response = apiService.uploadBackup(
                    repoId = repoId,
                    subfolder = subfolder,
                    sourcePath = sourcePath,
                    body = requestBody
                )

                if (response.isSuccessful) {
                    success = true
                    output = "Backup completed successfully"
                    Log.d(TAG, "Backup upload successful")
                } else {
                    val errorBody = response.errorBody()?.string() ?: "Unknown error"
                    errors.add("Upload failed: ${response.code()} - $errorBody")
                    output = errorBody
                    Log.e(TAG, "Backup upload failed: ${response.code()} - $errorBody")
                }

                reportProgress(runId, "finishing", 95, "Finalizing...")

            } finally {
                // Clean up temp file
                tempFile.delete()
            }

        } catch (e: Exception) {
            Log.e(TAG, "Backup failed", e)
            errors.add(e.message ?: "Backup failed")
            output = e.stackTraceToString().take(5000)

            reportProgress(runId, "failed", 0, e.message ?: "Backup failed")
        }

        // Report final result
        val finishedAt = Instant.now()
        val result = BackupResult(
            runId = runId,
            jobName = jobName,
            clientId = clientId,
            success = success,
            startedAt = DateTimeFormatter.ISO_INSTANT.format(startedAt),
            finishedAt = DateTimeFormatter.ISO_INSTANT.format(finishedAt),
            bytesTransferred = bytesTransferred,
            filesTransferred = filesTransferred,
            errors = errors,
            output = output.take(5000)
        )

        apiRepository.reportResult(result)

        // Update notification
        showCompletionNotification(jobName, success)

        if (success) Result.success() else Result.failure()
    }

    private fun parseExcludes(json: String): List<String> {
        return try {
            // Simple parsing - assumes format like ["*.tmp", ".git"]
            json.trim('[', ']')
                .split(",")
                .map { it.trim().trim('"') }
                .filter { it.isNotEmpty() }
        } catch (e: Exception) {
            emptyList()
        }
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
            .setContentTitle("Backing up")
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
            .setContentTitle(if (success) "Backup complete" else "Backup failed")
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
        private const val TAG = "BackupWorker"
        private const val NOTIFICATION_ID = 1001
        private const val COMPLETION_NOTIFICATION_ID = 1002
    }
}