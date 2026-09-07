package com.backer.android.util

import android.os.Environment
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.apache.commons.compress.archivers.tar.TarArchiveEntry
import org.apache.commons.compress.archivers.tar.TarArchiveInputStream
import org.apache.commons.compress.compressors.gzip.GzipCompressorInputStream
import java.io.BufferedInputStream
import java.io.EOFException
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.nio.file.Files
import java.util.UUID
import javax.inject.Inject
import javax.inject.Singleton

/** Extracts tar.gz archives for restore operations. */
@Singleton
class TarArchiveExtractor @Inject constructor() {

    suspend fun extractArchive(
        inputStream: InputStream,
        destDir: File,
        cleanRestore: Boolean = false,
        dryRun: Boolean = false,
        progressCallback: ((filesExtracted: Int, currentFile: String, bytesExtracted: Long) -> Unit)? = null
    ): ExtractResult = withContext(Dispatchers.IO) {
        var filesExtracted = 0
        var bytesExtracted = 0L
        var stagingDir: File? = null
        var previousDir: File? = null

        try {
            requireSafeDestination(destDir)
            val extractDir = if (cleanRestore && !dryRun) {
                File(destDir.parentFile, ".${destDir.name}.backer-restore-${UUID.randomUUID()}").also {
                    check(it.mkdir()) { "Unable to create restore staging directory" }
                    stagingDir = it
                }
            } else {
                destDir.also { if (!dryRun) check(it.exists() || it.mkdirs()) { "Unable to create destination directory" } }
            }

            BufferedInputStream(inputStream).use { bufferedIn ->
                GzipCompressorInputStream(bufferedIn).use { gzipIn ->
                    TarArchiveInputStream(gzipIn).use { tarIn ->
                        var entry: TarArchiveEntry? = tarIn.nextEntry

                        while (entry != null) {
                            val outputFile = safeOutputFile(extractDir, entry)

                            if (entry.isDirectory) {
                                if (!dryRun) {
                                    check(outputFile.exists() || outputFile.mkdirs()) { "Unable to create ${entry.name}" }
                                }
                            } else {
                                check(entry.isFile) { "Unsafe archive entry: ${entry.name}" }
                                if (!dryRun) {
                                    outputFile.parentFile?.let { check(it.exists() || it.mkdirs()) { "Unable to create parent for ${entry.name}" } }
                                }
                                if (!dryRun) {
                                    FileOutputStream(outputFile).use { copyEntry(tarIn, it, entry.size) }
                                    entry.lastModifiedDate?.let {
                                        check(outputFile.setLastModified(it.time)) { "Unable to restore timestamp for ${entry.name}" }
                                    }
                                } else {
                                    copyEntry(tarIn, null, entry.size)
                                }
                                filesExtracted++
                                bytesExtracted += entry.size
                                progressCallback?.invoke(filesExtracted, entry.name, bytesExtracted)
                            }

                            entry = tarIn.nextEntry
                        }
                    }
                }
            }

            check(filesExtracted > 0) { "Archive contains no files" }
            if (cleanRestore && !dryRun) {
                previousDir = File(destDir.parentFile, ".${destDir.name}.backer-previous-${UUID.randomUUID()}")
                if (destDir.exists()) check(destDir.renameTo(previousDir)) { "Unable to preserve existing destination" }
                check(stagingDir?.renameTo(destDir) == true) { "Unable to activate restored files" }
                val oldDir = previousDir
                previousDir = null
                check(oldDir?.deleteRecursively() != false) { "Unable to remove previous destination" }
                stagingDir = null
            }
            ExtractResult(true, filesExtracted, bytesExtracted, emptyList(), dryRun)
        } catch (e: CancellationException) {
            rollback(destDir, stagingDir, previousDir)
            throw e
        } catch (e: Exception) {
            rollback(destDir, stagingDir, previousDir)
            ExtractResult(false, filesExtracted, bytesExtracted, listOf(e.message ?: "Archive extraction failed"), dryRun)
        }
    }

    private fun safeOutputFile(destDir: File, entry: TarArchiveEntry): File {
        check(!entry.isSymbolicLink && !entry.isLink) { "Unsafe archive entry: ${entry.name}" }
        val output = File(destDir, entry.name)
        val dest = destDir.canonicalFile
        val target = output.canonicalFile
        check(target.path == dest.path || target.path.startsWith(dest.path + File.separator)) {
            "Unsafe archive entry: ${entry.name}"
        }
        var path: File? = output
        while (path != null && path.path != destDir.path) {
            check(!Files.isSymbolicLink(path.toPath())) { "Unsafe archive entry: ${entry.name}" }
            path = path.parentFile
        }
        return output
    }

    private fun copyEntry(input: InputStream, output: FileOutputStream?, size: Long) {
        check(size >= 0) { "Invalid archive entry size" }
        var remaining = size
        val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
        while (remaining > 0) {
            val count = input.read(buffer, 0, minOf(buffer.size.toLong(), remaining).toInt())
            if (count < 0) throw EOFException("Archive ended before entry was complete")
            output?.write(buffer, 0, count)
            remaining -= count
        }
    }

    private fun requireSafeDestination(destDir: File) {
        val absolute = destDir.absoluteFile
        check(absolute.parentFile != null && absolute.path != File.separator) { "Refusing to restore to filesystem root" }
        check(!absolute.exists() || absolute.isDirectory) { "Restore destination must be a directory" }
        val primaryStorage = runCatching { Environment.getExternalStorageDirectory().canonicalFile }.getOrNull()
        check(absolute.canonicalFile != primaryStorage) { "Refusing to restore to storage root" }
        var path: File? = absolute
        while (path != null) {
            check(!Files.isSymbolicLink(path.toPath())) { "Refusing symlink destination: ${destDir.path}" }
            path = path.parentFile
        }
    }

    private fun rollback(destDir: File, stagingDir: File?, previousDir: File?) {
        stagingDir?.deleteRecursively()
        if (previousDir?.exists() == true) {
            if (destDir.exists()) destDir.deleteRecursively()
            previousDir.renameTo(destDir)
        }
    }
}

data class ExtractResult(
    val success: Boolean,
    val filesExtracted: Int,
    val bytesExtracted: Long,
    val errors: List<String>,
    val dryRun: Boolean
)
