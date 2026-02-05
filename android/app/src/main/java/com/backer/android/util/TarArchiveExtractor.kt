package com.backer.android.util

import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.apache.commons.compress.archivers.tar.TarArchiveEntry
import org.apache.commons.compress.archivers.tar.TarArchiveInputStream
import org.apache.commons.compress.compressors.gzip.GzipCompressorInputStream
import java.io.BufferedInputStream
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Extracts tar.gz archives for restore operations.
 * Streams files from a compressed archive to the destination directory.
 */
@Singleton
class TarArchiveExtractor @Inject constructor() {

    /**
     * Extract a tar.gz archive from an input stream to a destination directory.
     *
     * @param inputStream The input stream of the tar.gz archive
     * @param destDir The destination directory to extract to
     * @param cleanRestore If true, delete existing files in destDir before extracting
     * @param dryRun If true, don't actually extract files, just report what would be done
     * @param progressCallback Called with progress updates
     * @return Result of the extraction
     */
    suspend fun extractArchive(
        inputStream: InputStream,
        destDir: File,
        cleanRestore: Boolean = false,
        dryRun: Boolean = false,
        progressCallback: ((filesExtracted: Int, currentFile: String, bytesExtracted: Long) -> Unit)? = null
    ): ExtractResult = withContext(Dispatchers.IO) {
        var filesExtracted = 0
        var bytesExtracted = 0L
        val errors = mutableListOf<String>()

        try {
            // Clean restore: delete existing contents
            if (cleanRestore && !dryRun) {
                if (destDir.exists()) {
                    Log.d(TAG, "Clean restore: deleting existing contents of ${destDir.absolutePath}")
                    destDir.deleteRecursively()
                }
            }

            // Ensure destination directory exists
            if (!dryRun && !destDir.exists()) {
                destDir.mkdirs()
            }

            BufferedInputStream(inputStream).use { bufferedIn ->
                GzipCompressorInputStream(bufferedIn).use { gzipIn ->
                    TarArchiveInputStream(gzipIn).use { tarIn ->
                        var entry: TarArchiveEntry? = tarIn.nextEntry

                        while (entry != null) {
                            val outputFile = File(destDir, entry.name)

                            // Security check: prevent path traversal attacks
                            if (!outputFile.canonicalPath.startsWith(destDir.canonicalPath)) {
                                errors.add("Skipping potentially unsafe entry: ${entry.name}")
                                entry = tarIn.nextEntry
                                continue
                            }

                            if (entry.isDirectory) {
                                if (!dryRun) {
                                    outputFile.mkdirs()
                                }
                                Log.d(TAG, "Created directory: ${entry.name}")
                            } else {
                                // Ensure parent directory exists
                                if (!dryRun) {
                                    outputFile.parentFile?.mkdirs()
                                }

                                if (!dryRun) {
                                    try {
                                        FileOutputStream(outputFile).use { fileOut ->
                                            tarIn.copyTo(fileOut)
                                        }

                                        // Restore file modification time if available
                                        entry.lastModifiedDate?.let { modTime ->
                                            outputFile.setLastModified(modTime.time)
                                        }
                                    } catch (e: Exception) {
                                        errors.add("Failed to extract ${entry.name}: ${e.message}")
                                        Log.w(TAG, "Failed to extract ${entry.name}", e)
                                        entry = tarIn.nextEntry
                                        continue
                                    }
                                }

                                filesExtracted++
                                bytesExtracted += entry.size
                                progressCallback?.invoke(filesExtracted, entry.name, bytesExtracted)

                                if (dryRun) {
                                    Log.d(TAG, "Would extract: ${entry.name} (${entry.size} bytes)")
                                } else {
                                    Log.d(TAG, "Extracted: ${entry.name} (${entry.size} bytes)")
                                }
                            }

                            entry = tarIn.nextEntry
                        }
                    }
                }
            }

            ExtractResult(
                success = true,
                filesExtracted = filesExtracted,
                bytesExtracted = bytesExtracted,
                errors = errors,
                dryRun = dryRun
            )
        } catch (e: Exception) {
            Log.e(TAG, "Failed to extract archive", e)
            ExtractResult(
                success = false,
                filesExtracted = filesExtracted,
                bytesExtracted = bytesExtracted,
                errors = errors + (e.message ?: "Archive extraction failed"),
                dryRun = dryRun
            )
        }
    }

    companion object {
        private const val TAG = "TarArchiveExtractor"
    }
}

data class ExtractResult(
    val success: Boolean,
    val filesExtracted: Int,
    val bytesExtracted: Long,
    val errors: List<String>,
    val dryRun: Boolean
)