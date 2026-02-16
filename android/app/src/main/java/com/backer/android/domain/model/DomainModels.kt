package com.backer.android.domain.model

/**
 * Agent credentials for authenticating with the Backer server.
 */
data class AgentCredentials(
    val serverUrl: String,
    val clientId: String,
    val clientSecret: String
)

/**
 * Connection status for the agent.
 */
enum class ConnectionStatus {
    CONNECTED,
    CONNECTING,
    DISCONNECTED,
    ERROR
}

/**
 * Result of a registration attempt.
 */
sealed class RegistrationResult {
    data class Success(
        val clientId: String,
        val serverVersion: String
    ) : RegistrationResult()

    data class Error(val message: String) : RegistrationResult()
}

/**
 * Result of a connection test.
 */
sealed class ConnectionResult {
    data class Success(val serverVersion: String) : ConnectionResult()
    data class Error(val message: String) : ConnectionResult()
}

/**
 * Agent status information.
 */
data class AgentStatus(
    val connectionStatus: ConnectionStatus,
    val clientId: String?,
    val serverUrl: String?,
    val lastBackup: Long?,
    val lastHeartbeat: Long?
)