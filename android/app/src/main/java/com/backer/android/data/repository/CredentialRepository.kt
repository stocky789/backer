package com.backer.android.data.repository

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import com.backer.android.domain.model.AgentCredentials
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Repository for securely storing and retrieving agent credentials.
 * Uses EncryptedSharedPreferences backed by Android Keystore.
 */
@Singleton
class CredentialRepository @Inject constructor(
    @ApplicationContext private val context: Context
) {
    private val masterKey = MasterKey.Builder(context)
        .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
        .build()

    private val encryptedPrefs: SharedPreferences by lazy {
        EncryptedSharedPreferences.create(
            context,
            PREFS_NAME,
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
        )
    }

    /**
     * Save agent credentials securely.
     */
    fun saveCredentials(serverUrl: String, clientId: String, clientSecret: String) {
        encryptedPrefs.edit()
            .putString(KEY_SERVER_URL, serverUrl)
            .putString(KEY_CLIENT_ID, clientId)
            .putString(KEY_CLIENT_SECRET, clientSecret)
            .apply()
    }

    /**
     * Get stored credentials, or null if not registered.
     */
    fun getCredentials(): AgentCredentials? {
        val serverUrl = encryptedPrefs.getString(KEY_SERVER_URL, null)
        val clientId = encryptedPrefs.getString(KEY_CLIENT_ID, null)
        val clientSecret = encryptedPrefs.getString(KEY_CLIENT_SECRET, null)

        return if (serverUrl != null && clientId != null && clientSecret != null) {
            AgentCredentials(serverUrl, clientId, clientSecret)
        } else {
            null
        }
    }

    /**
     * Check if the agent is registered.
     */
    fun isRegistered(): Boolean {
        return getCredentials() != null
    }

    /**
     * Get the server URL, or null if not registered.
     */
    fun getServerUrl(): String? {
        return encryptedPrefs.getString(KEY_SERVER_URL, null)
    }

    /**
     * Get the client ID, or null if not registered.
     */
    fun getClientId(): String? {
        return encryptedPrefs.getString(KEY_CLIENT_ID, null)
    }

    /**
     * Clear all stored credentials (logout/disconnect).
     */
    fun clearCredentials() {
        encryptedPrefs.edit().clear().apply()
    }

    /**
     * Update last heartbeat timestamp.
     */
    fun updateLastHeartbeat(timestamp: Long) {
        encryptedPrefs.edit()
            .putLong(KEY_LAST_HEARTBEAT, timestamp)
            .apply()
    }

    /**
     * Get last heartbeat timestamp.
     */
    fun getLastHeartbeat(): Long? {
        val value = encryptedPrefs.getLong(KEY_LAST_HEARTBEAT, -1)
        return if (value >= 0) value else null
    }

    /**
     * Update last backup timestamp.
     */
    fun updateLastBackup(timestamp: Long) {
        encryptedPrefs.edit()
            .putLong(KEY_LAST_BACKUP, timestamp)
            .apply()
    }

    /**
     * Get last backup timestamp.
     */
    fun getLastBackup(): Long? {
        val value = encryptedPrefs.getLong(KEY_LAST_BACKUP, -1)
        return if (value >= 0) value else null
    }

    companion object {
        private const val PREFS_NAME = "backer_secure_prefs"
        private const val KEY_SERVER_URL = "server_url"
        private const val KEY_CLIENT_ID = "client_id"
        private const val KEY_CLIENT_SECRET = "client_secret"
        private const val KEY_LAST_HEARTBEAT = "last_heartbeat"
        private const val KEY_LAST_BACKUP = "last_backup"
    }
}