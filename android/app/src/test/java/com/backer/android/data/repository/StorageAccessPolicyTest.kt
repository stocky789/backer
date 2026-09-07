package com.backer.android.data.repository

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class StorageAccessPolicyTest {
    @Test fun android11PlusRequiresAllFilesAccess() {
        assertFalse(StorageAccessPolicy.hasUserFileAccess(30, false, true))
        assertTrue(StorageAccessPolicy.hasUserFileAccess(30, true, false))
    }

    @Test fun legacyAndroidUsesReadPermission() {
        assertFalse(StorageAccessPolicy.hasUserFileAccess(29, false, false))
        assertTrue(StorageAccessPolicy.hasUserFileAccess(29, false, true))
    }
}
