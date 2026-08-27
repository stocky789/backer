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
