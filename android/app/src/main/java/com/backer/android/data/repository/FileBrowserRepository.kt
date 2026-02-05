package com.backer.android.data.repository

import android.content.Context
import android.os.Environment
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
    /**
     * Browse a directory and return its contents.
     * This is called when the server sends a browse_filesystem command.
     */
    suspend fun browse(path: String): BrowseResults = withContext(Dispatchers.IO) {
        try {
            if (path.isEmpty()) {
                // Return root entries (common Android directories)
                return@withContext getRootEntries()
            }

            val directory = File(path)

            if (!directory.exists()) {
                return@withContext BrowseResults(
                    success = false,
                    path = path,
                    entries = emptyList(),
                    error = "Path does not exist: $path"
                )
            }

            if (!directory.isDirectory) {
                return@withContext BrowseResults(
                    success = false,
                    path = path,
                    entries = emptyList(),
                    error = "Path is not a directory: $path"
                )
            }

            if (!directory.canRead()) {
                return@withContext BrowseResults(
                    success = false,
                    path = path,
                    entries = emptyList(),
                    error = "Permission denied: $path"
                )
            }

            val entries = mutableListOf<FileEntry>()
            val dirs = mutableListOf<FileEntry>()
            val files = mutableListOf<FileEntry>()

            directory.listFiles()?.take(500)?.forEach { file ->
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
                    // Skip files we can't access
                }
            }

            // Sort and combine (directories first)
            dirs.sortBy { it.name.lowercase() }
            files.sortBy { it.name.lowercase() }
            entries.addAll(dirs)
            entries.addAll(files)

            BrowseResults(
                success = true,
                path = path,
                entries = entries.take(200)
            )
        } catch (e: Exception) {
            BrowseResults(
                success = false,
                path = path,
                entries = emptyList(),
                error = e.message ?: "Browse failed"
            )
        }
    }

    /**
     * Get root entries showing common Android storage locations.
     */
    private fun getRootEntries(): BrowseResults {
        val entries = mutableListOf<FileEntry>()

        // Internal storage root
        val storageDir = Environment.getExternalStorageDirectory()
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
            }
        }

        // App-specific external storage
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
            }
        }

        return BrowseResults(
            success = true,
            path = "",
            entries = entries
        )
    }
}