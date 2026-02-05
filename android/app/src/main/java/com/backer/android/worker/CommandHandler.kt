package com.backer.android.worker

import android.content.Context
import android.util.Log
import androidx.work.Data
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import com.backer.android.data.api.models.BackupCommand
import com.backer.android.data.api.models.BrowseResults
import com.backer.android.data.repository.BackerApiRepository
import com.backer.android.data.repository.FileBrowserRepository
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Handles commands received from the Backer server.
 */
@Singleton
class CommandHandler @Inject constructor(
    @ApplicationContext private val context: Context,
    private val apiRepository: BackerApiRepository,
    private val fileBrowserRepository: FileBrowserRepository
) {
    private val workManager = WorkManager.getInstance(context)
    private val scope = CoroutineScope(Dispatchers.IO)

    /**
     * Handle a command from the server.
     */
    fun handle(command: BackupCommand) {
        Log.d(TAG, "Handling command: ${command.commandType} (id=${command.id})")

        when (command.commandType) {
            "backup" -> handleBackupCommand(command)
            "restore" -> handleRestoreCommand(command)
            "browse_filesystem" -> handleBrowseCommand(command)
            else -> Log.w(TAG, "Unknown command type: ${command.commandType}")
        }
    }

    private fun handleBackupCommand(command: BackupCommand) {
        val payload = command.payload

        val jobName = payload["job_name"]?.jsonPrimitive?.content ?: "unknown"
        val runId = payload["run_id"]?.jsonPrimitive?.content ?: ""
        val sourcePath = payload["source_path"]?.jsonPrimitive?.content ?: ""
        val destinationPath = payload["destination_path"]?.jsonPrimitive?.content ?: ""
        val backend = payload["backend"]?.jsonPrimitive?.content ?: "proxy"

        // Extract excludes as a comma-separated string
        val excludes = payload["excludes"]?.toString() ?: "[]"

        Log.d(TAG, "Backup command: job=$jobName, source=$sourcePath, dest=$destinationPath, backend=$backend")

        // Android only supports proxy backend - server should route through proxy for all repo types
        if (backend != "proxy") {
            Log.e(TAG, "Unsupported backend '$backend' for Android. Only 'proxy' backend is supported. " +
                "Please ensure the Backer server is updated to automatically route Android agents through proxy.")
            // Report error back to server
            scope.launch {
                try {
                    apiRepository.reportProgress(
                        com.backer.android.data.api.models.ProgressReport(
                            runId = runId,
                            status = "failed",
                            progressPercent = 0,
                            message = "Android does not support '$backend' backend. Only proxy backend is supported."
                        )
                    )
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to report error", e)
                }
            }
            return
        }

        // Create work request for backup
        val inputData = Data.Builder()
            .putString("job_name", jobName)
            .putString("run_id", runId)
            .putString("source_path", sourcePath)
            .putString("destination_path", destinationPath)
            .putString("backend", backend)
            .putString("excludes", excludes)
            .putInt("command_id", command.id)
            .build()

        val backupRequest = OneTimeWorkRequestBuilder<BackupWorker>()
            .setInputData(inputData)
            .build()

        workManager.enqueueUniqueWork(
            "backup_$jobName",
            ExistingWorkPolicy.REPLACE,
            backupRequest
        )

        Log.d(TAG, "Backup work enqueued for job: $jobName")
    }

    private fun handleRestoreCommand(command: BackupCommand) {
        val payload = command.payload

        val jobName = payload["job_name"]?.jsonPrimitive?.content ?: "unknown"
        val runId = payload["run_id"]?.jsonPrimitive?.content ?: ""
        val sourcePath = payload["source_path"]?.jsonPrimitive?.content ?: ""
        val destinationPath = payload["destination_path"]?.jsonPrimitive?.content ?: ""
        val backend = payload["backend"]?.jsonPrimitive?.content ?: "proxy"
        val snapshot = payload["snapshot"]?.jsonPrimitive?.content
        val cleanRestore = payload["clean_restore"]?.jsonPrimitive?.boolean ?: false
        val dryRun = payload["dry_run"]?.jsonPrimitive?.boolean ?: false

        Log.d(TAG, "Restore command: job=$jobName, source=$sourcePath, dest=$destinationPath, backend=$backend")

        // Android only supports proxy backend - server should route through proxy for all repo types
        if (backend != "proxy") {
            Log.e(TAG, "Unsupported backend '$backend' for Android restore. Only 'proxy' backend is supported. " +
                "Please ensure the Backer server is updated to automatically route Android agents through proxy.")
            // Report error back to server
            scope.launch {
                try {
                    apiRepository.reportProgress(
                        com.backer.android.data.api.models.ProgressReport(
                            runId = runId,
                            status = "failed",
                            progressPercent = 0,
                            message = "Android does not support '$backend' backend for restore. Only proxy backend is supported."
                        )
                    )
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to report error", e)
                }
            }
            return
        }

        // Create work request for restore
        val inputData = Data.Builder()
            .putString("job_name", jobName)
            .putString("run_id", runId)
            .putString("source_path", sourcePath)
            .putString("destination_path", destinationPath)
            .putString("snapshot", snapshot)
            .putBoolean("clean_restore", cleanRestore)
            .putBoolean("dry_run", dryRun)
            .putInt("command_id", command.id)
            .build()

        val restoreRequest = OneTimeWorkRequestBuilder<RestoreWorker>()
            .setInputData(inputData)
            .build()

        workManager.enqueueUniqueWork(
            "restore_$jobName",
            ExistingWorkPolicy.REPLACE,
            restoreRequest
        )

        Log.d(TAG, "Restore work enqueued for job: $jobName")
    }

    private fun handleBrowseCommand(command: BackupCommand) {
        val payload = command.payload

        val requestId = payload["request_id"]?.jsonPrimitive?.content
        val path = payload["path"]?.jsonPrimitive?.content ?: ""

        if (requestId == null) {
            Log.e(TAG, "Browse command missing request_id")
            return
        }

        Log.d(TAG, "Browse command: requestId=$requestId, path=$path")

        // Execute browse in background and report results
        scope.launch {
            try {
                val results = fileBrowserRepository.browse(path)
                apiRepository.reportBrowseResults(requestId, results)
                Log.d(TAG, "Browse results reported: ${results.entries.size} entries")
            } catch (e: Exception) {
                Log.e(TAG, "Failed to handle browse command", e)
                // Report error
                apiRepository.reportBrowseResults(
                    requestId,
                    BrowseResults(
                        success = false,
                        path = path,
                        entries = emptyList(),
                        error = e.message ?: "Browse failed"
                    )
                )
            }
        }
    }

    companion object {
        private const val TAG = "CommandHandler"
    }
}