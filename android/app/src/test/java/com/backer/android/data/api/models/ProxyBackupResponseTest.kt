package com.backer.android.data.api.models

import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class ProxyBackupResponseTest {
    private val json = Json { ignoreUnknownKeys = true }

    @Test fun serverDeclaredFailureIsNotAccepted() {
        val response = json.decodeFromString<ProxyBackupResponse>("{\"success\":false,\"error\":\"archive rejected\"}")

        assertFalse(response.success)
        assertEquals("archive rejected", response.error)
    }

    @Test fun snapshotIdIsAvailableForResultReporting() {
        val response = json.decodeFromString<ProxyBackupResponse>("{\"success\":true,\"snapshot_id\":\"snapshot-42\"}")
        val result = BackupResult("run", "job", "client", true, "start", "finish", snapshotId = response.snapshotId)

        assertEquals("snapshot-42", result.snapshotId)
    }
}
