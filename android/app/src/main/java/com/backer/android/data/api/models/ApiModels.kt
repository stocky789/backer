package com.backer.android.data.api.models

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

/**
 * Request to register a new agent with the server.
 */
@Serializable
data class RegisterRequest(
    val hostname: String,
    val version: String,
    @SerialName("os_info") val osInfo: String,
    val tags: List<String> = emptyList()
)

/**
 * Response from agent registration.
 */
@Serializable
data class RegisterResponse(
    @SerialName("client_id") val clientId: String,
    @SerialName("client_secret") val clientSecret: String,
    @SerialName("server_version") val serverVersion: String
)

/**
 * Health check response.
 */
@Serializable
data class HealthResponse(
    val status: String,
    val version: String? = null
)

/**
 * Heartbeat request sent periodically to the server.
 */
@Serializable
data class HeartbeatRequest(
    @SerialName("client_id") val clientId: String,
    val status: String = "online",
    @SerialName("current_job") val currentJob: String? = null,
    @SerialName("jobs_completed") val jobsCompleted: Int = 0,
    @SerialName("jobs_failed") val jobsFailed: Int = 0
)

/**
 * Response from heartbeat, may contain commands to execute.
 */
@Serializable
data class HeartbeatResponse(
    val status: String,
    val commands: List<BackupCommand> = emptyList()
)

/**
 * A command received from the server.
 */
@Serializable
data class BackupCommand(
    val id: Int,
    @SerialName("command_type") val commandType: String,
    val payload: JsonObject
)

/**
 * Progress report sent during backup execution.
 */
@Serializable
data class ProgressReport(
    @SerialName("run_id") val runId: String,
    val status: String? = null,
    @SerialName("progress_percent") val progressPercent: Int? = null,
    @SerialName("current_file") val currentFile: String? = null,
    @SerialName("bytes_processed") val bytesProcessed: Long? = null,
    @SerialName("files_processed") val filesProcessed: Int? = null,
    @SerialName("total_bytes") val totalBytes: Long? = null,
    @SerialName("total_files") val totalFiles: Int? = null,
    val message: String? = null
)

/**
 * Final backup result reported to the server.
 */
@Serializable
data class BackupResult(
    @SerialName("run_id") val runId: String,
    @SerialName("job_name") val jobName: String,
    @SerialName("client_id") val clientId: String,
    val success: Boolean,
    @SerialName("started_at") val startedAt: String,
    @SerialName("finished_at") val finishedAt: String,
    @SerialName("bytes_transferred") val bytesTransferred: Long = 0,
    @SerialName("files_transferred") val filesTransferred: Int = 0,
    val errors: List<String> = emptyList(),
    val output: String = "",
    @SerialName("snapshot_id") val snapshotId: String? = null
)

/**
 * Browse filesystem results.
 */
@Serializable
data class BrowseResults(
    val success: Boolean,
    val path: String,
    val entries: List<FileEntry> = emptyList(),
    val error: String? = null
)

/**
 * A file or directory entry.
 */
@Serializable
data class FileEntry(
    val name: String,
    val path: String,
    @SerialName("is_dir") val isDir: Boolean,
    val size: Long = 0
)

/**
 * Acknowledge command response.
 */
@Serializable
data class AckResponse(
    val status: String
)

/**
 * Final restore result reported to the server.
 */
@Serializable
data class RestoreResult(
    @SerialName("run_id") val runId: String,
    @SerialName("job_name") val jobName: String,
    @SerialName("client_id") val clientId: String,
    val success: Boolean,
    @SerialName("started_at") val startedAt: String,
    @SerialName("finished_at") val finishedAt: String,
    @SerialName("bytes_restored") val bytesRestored: Long = 0,
    @SerialName("files_restored") val filesRestored: Int = 0,
    val errors: List<String> = emptyList(),
    val output: String = ""
)