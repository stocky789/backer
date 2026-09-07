package com.backer.android.util

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

class ArchiveResultTest {
    @Test fun partialArchiveIsNotComplete() {
        assertFalse(ArchiveResult(true, 1, 10, 10, listOf("unreadable.txt")).isComplete)
        assertTrue(ArchiveResult(true, 1, 10, 10, emptyList()).isComplete)
    }

    @Test fun unlistableSourceFailsInsteadOfCreatingAnEmptyArchive() = runBlocking {
        val parent = Files.createTempDirectory("backer-archive").toFile()
        val sourceFile = File(parent, "not-a-directory").apply { writeText("data") }
        val archive = File(parent, "backup.tar.gz")

        val result = TarArchiveCreator().createArchive(sourceFile, archive)

        assertFalse(result.success)
        assertTrue(result.errors.single().contains("Cannot list directory"))
        parent.deleteRecursively()
    }
}
