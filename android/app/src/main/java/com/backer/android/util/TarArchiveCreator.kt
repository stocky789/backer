package com.backer.android.util

import android.util.Log
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.apache.commons.compress.archivers.tar.TarArchiveEntry
import org.apache.commons.compress.archivers.tar.TarArchiveOutputStream
import org.apache.commons.compress.compressors.gzip.GzipCompressorOutputStream
import java.io.BufferedOutputStream
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.util.Date
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Creates tar.gz archives for backup uploads.
 * Streams files into a compressed archive for efficient transfer to the server.
 */
@Singleton
class TarArchiveCreator @Inject constructor() {

    /**
     * Create a tar.gz archive from a source directory.
     *
     * @param sourceDir The directory to archive
     * @param outputFile The output tar.gz file
     * @param excludePatterns Patterns to exclude (e.g., "*.tmp", ".git")
     * @param progressCallback Called with progress updates
     * @return Total number of files archived
     */
    suspend fun createArchive(
        sourceDir: File,
        outputFile: File,
        excludePatterns: List<String> = emptyList(),
        progressCallback: ((filesProcessed: Int, currentFile: String, bytesProcessed: Long) -> Unit)? = null
    ): ArchiveResult = withContext(Dispatchers.IO) {
        var filesProcessed = 0
        var bytesProcessed = 0L
        val errors = mutableListOf<String>()

        try {
            FileOutputStream(outputFile).use { fileOut ->
                BufferedOutputStream(fileOut).use { bufferedOut ->
                    GzipCompressorOutputStream(bufferedOut).use { gzipOut ->
                        TarArchiveOutputStream(gzipOut).use { tarOut ->
                            tarOut.setLongFileMode(TarArchiveOutputStream.LONGFILE_POSIX)
                            tarOut.setBigNumberMode(TarArchiveOutputStream.BIGNUMBER_POSIX)

                            addDirectoryToArchive(
                                tarOut = tarOut,
                                sourceDir = sourceDir,
                                basePath = "",
                                excludePatterns = excludePatterns,
                                onFile = { relativePath, size ->
                                    filesProcessed++
                                    bytesProcessed += size
                                    progressCallback?.invoke(filesProcessed, relativePath, bytesProcessed)
                                },
                                onError = { error ->
                                    errors.add(error)
                                    Log.w(TAG, "Archive error: $error")
                                }
                            )
                        }
                    }
                }
            }

            ArchiveResult(
                success = errors.isEmpty(),
                filesProcessed = filesProcessed,
                bytesProcessed = bytesProcessed,
                archiveSize = outputFile.length(),
                errors = errors
            )
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            Log.e(TAG, "Failed to create archive", e)
            ArchiveResult(
                success = false,
                filesProcessed = filesProcessed,
                bytesProcessed = bytesProcessed,
                archiveSize = 0,
                errors = errors + (e.message ?: "Archive creation failed")
            )
        }
    }

    private fun addDirectoryToArchive(
        tarOut: TarArchiveOutputStream,
        sourceDir: File,
        basePath: String,
        excludePatterns: List<String>,
        onFile: (String, Long) -> Unit,
        onError: (String) -> Unit
    ) {
        val files = sourceDir.listFiles()
        if (files == null) {
            onError("Cannot list directory: ${sourceDir.path}")
            return
        }

        for (file in files) {
            val relativePath = if (basePath.isEmpty()) file.name else "$basePath/${file.name}"

            // Check exclusions
            if (shouldExclude(relativePath, file.name, excludePatterns)) {
                continue
            }

            try {
                if (file.isDirectory) {
                    // Add directory entry
                    val dirEntry = TarArchiveEntry(file, "$relativePath/")
                    tarOut.putArchiveEntry(dirEntry)
                    tarOut.closeArchiveEntry()

                    // Recurse into directory
                    addDirectoryToArchive(
                        tarOut = tarOut,
                        sourceDir = file,
                        basePath = relativePath,
                        excludePatterns = excludePatterns,
                        onFile = onFile,
                        onError = onError
                    )
                } else if (!file.isFile) {
                    onError("Unsupported source entry: $relativePath")
                } else if (!file.canRead()) {
                    onError("Cannot read file: $relativePath")
                } else {
                    // Add file entry
                    val entry = TarArchiveEntry(file, relativePath).apply {
                        size = file.length()
                        modTime = Date(file.lastModified())
                    }
                    tarOut.putArchiveEntry(entry)

                    // Copy file content
                    FileInputStream(file).use { input ->
                        input.copyTo(tarOut)
                    }

                    tarOut.closeArchiveEntry()
                    onFile(relativePath, file.length())
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                onError("Failed to add $relativePath: ${e.message}")
            }
        }
    }

    private fun shouldExclude(relativePath: String, fileName: String, patterns: List<String>): Boolean {
        for (pattern in patterns) {
            when {
                // Exact match
                pattern == fileName -> return true
                // Glob pattern with *
                pattern.startsWith("*") && fileName.endsWith(pattern.substring(1)) -> return true
                pattern.endsWith("*") && fileName.startsWith(pattern.dropLast(1)) -> return true
                // Directory pattern
                pattern.endsWith("/") && relativePath.startsWith(pattern.dropLast(1)) -> return true
            }
        }
        return false
    }

    companion object {
        private const val TAG = "TarArchiveCreator"
    }
}

data class ArchiveResult(
    val success: Boolean,
    val filesProcessed: Int,
    val bytesProcessed: Long,
    val archiveSize: Long,
    val errors: List<String>
) {
    val isComplete: Boolean get() = success && errors.isEmpty()
}
