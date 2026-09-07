package com.backer.android.util

import kotlinx.coroutines.runBlocking
import org.apache.commons.compress.archivers.tar.TarArchiveEntry
import org.apache.commons.compress.archivers.tar.TarArchiveOutputStream
import org.apache.commons.compress.compressors.gzip.GzipCompressorOutputStream
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.nio.file.Files

class TarArchiveExtractorTest {
    @Test fun cleanRestoreKeepsOriginalWhenArchiveIsUnsafe() = runBlocking {
        val parent = Files.createTempDirectory("backer-restore").toFile()
        val destination = File(parent, "restore").apply { mkdir() }
        File(destination, "original.txt").writeText("keep")

        val result = TarArchiveExtractor().extractArchive(
            archive("../escape.txt" to "nope"), destination, cleanRestore = true
        )

        assertFalse(result.success)
        assertEquals("keep", File(destination, "original.txt").readText())
        assertFalse(File(parent, "escape.txt").exists())
        parent.deleteRecursively()
    }

    @Test fun cleanRestoreReplacesOnlyAfterSuccessfulExtraction() = runBlocking {
        val parent = Files.createTempDirectory("backer-restore").toFile()
        val destination = File(parent, "restore").apply { mkdir() }
        File(destination, "original.txt").writeText("old")

        val result = TarArchiveExtractor().extractArchive(
            archive("restored.txt" to "new"), destination, cleanRestore = true
        )

        assertTrue(result.success)
        assertFalse(File(destination, "original.txt").exists())
        assertEquals("new", File(destination, "restored.txt").readText())
        parent.deleteRecursively()
    }

    @Test fun refusesFileDestinationAndDryRunDoesNotCreateIt() = runBlocking {
        val parent = Files.createTempDirectory("backer-restore").toFile()
        val fileDestination = File(parent, "restore-file").apply { writeText("keep") }
        val missingDestination = File(parent, "missing")

        assertFalse(TarArchiveExtractor().extractArchive(archive("restored.txt" to "new"), fileDestination).success)
        assertTrue(TarArchiveExtractor().extractArchive(archive("restored.txt" to "new"), missingDestination, dryRun = true).success)
        assertFalse(missingDestination.exists())
        assertEquals("keep", fileDestination.readText())
        parent.deleteRecursively()
    }

    private fun archive(vararg files: Pair<String, String>): ByteArrayInputStream {
        val bytes = ByteArrayOutputStream()
        GzipCompressorOutputStream(bytes).use { gzip ->
            TarArchiveOutputStream(gzip).use { tar ->
                files.forEach { (name, content) ->
                    val data = content.toByteArray()
                    tar.putArchiveEntry(TarArchiveEntry(name).apply { size = data.size.toLong() })
                    tar.write(data)
                    tar.closeArchiveEntry()
                }
                tar.finish()
            }
        }
        return ByteArrayInputStream(bytes.toByteArray())
    }
}
