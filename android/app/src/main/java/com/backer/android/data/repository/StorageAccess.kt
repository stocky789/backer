package com.backer.android.data.repository

import android.content.Context
import android.os.Build
import android.os.Environment
import androidx.core.content.ContextCompat
import android.Manifest
import android.content.pm.PackageManager
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton

/** Access required for unattended raw-path backup and restore. */
@Singleton
class StorageAccess @Inject constructor(
    @ApplicationContext private val context: Context
) {
    fun hasUserFileAccess(): Boolean = StorageAccessPolicy.hasUserFileAccess(
        sdkInt = Build.VERSION.SDK_INT,
        allFilesAccessGranted = Build.VERSION.SDK_INT >= Build.VERSION_CODES.R &&
            Environment.isExternalStorageManager(),
        legacyReadGranted = ContextCompat.checkSelfPermission(
            context,
            Manifest.permission.READ_EXTERNAL_STORAGE
        ) == PackageManager.PERMISSION_GRANTED
    )

    fun denialMessage(): String =
        "All files access is required for unattended backup and restore. Enable it in Backer settings."
}

internal object StorageAccessPolicy {
    fun hasUserFileAccess(
        sdkInt: Int,
        allFilesAccessGranted: Boolean,
        legacyReadGranted: Boolean
    ): Boolean = if (sdkInt >= Build.VERSION_CODES.R) allFilesAccessGranted else legacyReadGranted
}
