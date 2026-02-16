package com.backer.android.data.repository

import android.content.Context
import android.os.Build
import android.os.Environment
import android.util.Log
import com.backer.android.data.api.models.BrowseResults
import com.backer.android.data.api.models.FileEntry
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Repository for browsing the Android filesystem.
 * Used to respond to browse_filesystem commands from the server.
 */
@Singleton
class FileBrowserRepository @Inject constructor(
    @ApplicationContext private val context: Context
) {
    companion object {
        private const val TAG = "FileBrowserRepository"
    }
    /**
     * Browse a directory and return its contents.
     * This is called when the server sends a browse_filesystem command.
     */
    suspend fun browse(path: String): BrowseResults = withContext(Dispatchers.IO) {
        Log.d(TAG, "Browse requested for path: '$path'")

        try {
            if (path.isEmpty()) {
                // Return root entries (common Android directories)
                Log.d(TAG, "Empty path - returning root entries")
                return@withContext getRootEntries()
            }

            val directory = File(path)
            Log.d(TAG, "Checking directory: ${directory.absolutePath}")
            Log.d(TAG, "Directory exists: ${directory.exists()}, isDirectory: ${directory.isDirectory}, canRead: ${directory.canRead()}")

            if (!directory.exists()) {
                Log.w(TAG, "Path does not exist: $path")
                return@withContext BrowseResults(
                    success = false,
                    path = path,
                    entries = emptyList(),
                    error = "Path does not exist: $path"
                )
            }

            if (!directory.isDirectory) {
                Log.w(TAG, "Path is not a directory: $path")
                return@withContext BrowseResults(
                    success = false,
                    path = path,
                    entries = emptyList(),
                    error = "Path is not a directory: $path"
                )
            }

            if (!directory.canRead()) {
                Log.w(TAG, "Permission denied for path: $path")
                return@withContext BrowseResults(
                    success = false,
                    path = path,
                    entries = emptyList(),
                    error = "Permission denied: $path. Please grant storage permissions to the Backer app."
                )
            }

            val dirs = mutableListOf<FileEntry>()
            val files = mutableListOf<FileEntry>()

            val fileList = directory.listFiles()
            Log.d(TAG, "listFiles returned: ${fileList?.size ?: "null"} items")

            fileList?.take(500)?.forEach { file ->
                try {
                    val entry = FileEntry(
                        name = file.name,
                        path = file.absolutePath,
                        isDir = file.isDirectory,
                        size = if (file.isFile) file.length() else 0
                    )

                    if (file.isDirectory) {
                        dirs.add(entry)
                    } else {
                        files.add(entry)
                    }
                } catch (e: SecurityException) {
                    Log.d(TAG, "Skipping inaccessible file: ${file.name}")
                }
            }

            // Sort and combine (directories first)
            dirs.sortBy { it.name.lowercase() }
            files.sortBy { it.name.lowercase() }
            val entries = (dirs + files).take(200)

            Log.i(TAG, "Browse completed: ${entries.size} entries (${dirs.size} dirs, ${files.size} files)")

            BrowseResults(
                success = true,
                path = path,
                entries = entries
            )
        } catch (e: Exception) {
            Log.e(TAG, "Browse failed with exception: ${e.message}", e)
            BrowseResults(
                success = false,
                path = path,
                entries = emptyList(),
                error = "Browse failed: ${e.message ?: e::class.simpleName}"
            )
        }
    }

    /**
     * Get root entries showing common Android storage locations.
     */
    private fun getRootEntries(): BrowseResults {
        Log.d(TAG, "Getting root entries")
        Log.d(TAG, "Android SDK: ${Build.VERSION.SDK_INT}, External storage state: ${Environment.getExternalStorageState()}")

        val entries = mutableListOf<FileEntry>()

        // Internal storage root
        val storageDir = Environment.getExternalStorageDirectory()
        Log.d(TAG, "External storage dir: ${storageDir.absolutePath}, exists: ${storageDir.exists()}, canRead: ${storageDir.canRead()}")

        if (storageDir.exists() && storageDir.canRead()) {
            entries.add(
                FileEntry(
                    name = "Internal Storage",
                    path = storageDir.absolutePath,
                    isDir = true,
                    size = 0
                )
            )
        }

        // Common directories
        val commonDirs = listOf(
            Environment.DIRECTORY_DCIM to "Camera (DCIM)",
            Environment.DIRECTORY_PICTURES to "Pictures",
            Environment.DIRECTORY_DOWNLOADS to "Downloads",
            Environment.DIRECTORY_DOCUMENTS to "Documents",
            Environment.DIRECTORY_MUSIC to "Music",
            Environment.DIRECTORY_MOVIES to "Movies"
        )

        commonDirs.forEach { (dirType, name) ->
            try {
                val dir = Environment.getExternalStoragePublicDirectory(dirType)
                if (dir.exists() && dir.canRead()) {
                    entries.add(
                        FileEntry(
                            name = name,
                            path = dir.absolutePath,
                            isDir = true,
                            size = 0
                        )
                    )
                    Log.d(TAG, "Added accessible directory: $name (${dir.absolutePath})")
                } else {
                    Log.d(TAG, "Directory not accessible: $name (exists=${dir.exists()}, canRead=${dir.canRead()})")
                }
            } catch (e: Exception) {
                Log.w(TAG, "Failed to check directory $name: ${e.message}")
            }
        }

        // App-specific external storage (always accessible without permissions)
        context.getExternalFilesDir(null)?.let { appDir ->
            if (appDir.exists()) {
                entries.add(
                    FileEntry(
                        name = "App Data",
                        path = appDir.absolutePath,
                        isDir = true,
                        size = 0
                    )
                )
                Log.d(TAG, "Added App Data directory: ${appDir.absolutePath}")
            }
        }

        Log.i(TAG, "Root entries: ${entries.size} accessible directories")

        // If no directories are accessible, return an error with guidance
        if (entries.isEmpty()) {
            Log.w(TAG, "No directories accessible - storage permissions may be needed")
            return BrowseResults(
                success = false,
                path = "",
                entries = emptyList(),
                error = "No storage directories accessible. Please grant storage permissions to the Backer app in Android Settings."
            )
        }

        return BrowseResults(
            success = true,
            path = "",
            entries = entries
        )
    }
}