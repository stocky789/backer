package com.backer.android.presentation.setup

import android.os.Build
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.backer.android.BuildConfig
import com.backer.android.data.api.models.RegisterRequest
import com.backer.android.data.repository.CredentialRepository
import com.backer.android.di.ApiServiceFactory
import com.backer.android.worker.WorkManagerScheduler
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

@HiltViewModel
class SetupViewModel @Inject constructor(
    private val credentialRepository: CredentialRepository,
    private val apiServiceFactory: ApiServiceFactory,
    private val workManagerScheduler: WorkManagerScheduler
) : ViewModel() {

    private val _uiState = MutableStateFlow(SetupUiState())
    val uiState: StateFlow<SetupUiState> = _uiState.asStateFlow()

    private val _isRegistered = MutableStateFlow(credentialRepository.isRegistered())
    val isRegistered: StateFlow<Boolean> = _isRegistered.asStateFlow()

    /**
     * Reset the setup screen state. Called when returning to setup after disconnect.
     */
    fun resetState() {
        _uiState.value = SetupUiState()
        _isRegistered.value = credentialRepository.isRegistered()
    }

    /**
     * Check and update registration status. Called when screen becomes visible.
     */
    fun checkRegistrationStatus() {
        _isRegistered.value = credentialRepository.isRegistered()
    }

    fun updateServerUrl(url: String) {
        _uiState.value = _uiState.value.copy(
            serverUrl = url,
            connectionStatus = ConnectionStatus.IDLE,
            errorMessage = null
        )
    }

    fun testConnection() {
        val serverUrl = normalizeUrl(_uiState.value.serverUrl)

        _uiState.value = _uiState.value.copy(
            serverUrl = serverUrl,
            connectionStatus = ConnectionStatus.TESTING,
            errorMessage = null
        )

        viewModelScope.launch {
            try {
                val apiService = apiServiceFactory.create(serverUrl)
                val response = apiService.healthCheck()

                if (response.status == "ok") {
                    _uiState.value = _uiState.value.copy(
                        connectionStatus = ConnectionStatus.SUCCESS,
                        serverVersion = response.version,
                        errorMessage = null
                    )
                } else {
                    _uiState.value = _uiState.value.copy(
                        connectionStatus = ConnectionStatus.ERROR,
                        errorMessage = "Unexpected server response"
                    )
                }
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(
                    connectionStatus = ConnectionStatus.ERROR,
                    errorMessage = e.message ?: "Connection failed"
                )
            }
        }
    }

    fun register() {
        val serverUrl = normalizeUrl(_uiState.value.serverUrl)

        _uiState.value = _uiState.value.copy(
            serverUrl = serverUrl,
            registrationStatus = RegistrationStatus.REGISTERING,
            errorMessage = null
        )

        viewModelScope.launch {
            try {
                val apiService = apiServiceFactory.create(serverUrl)

                val request = RegisterRequest(
                    hostname = Build.MODEL,
                    version = BuildConfig.VERSION_NAME,
                    osInfo = "Android ${Build.VERSION.RELEASE}",
                    tags = listOf("android")
                )

                val response = apiService.register(request)

                // Save credentials
                credentialRepository.saveCredentials(
                    serverUrl = serverUrl,
                    clientId = response.clientId,
                    clientSecret = response.clientSecret
                )

                _uiState.value = _uiState.value.copy(
                    registrationStatus = RegistrationStatus.SUCCESS,
                    clientId = response.clientId,
                    errorMessage = null
                )

                // Start heartbeat worker
                workManagerScheduler.startHeartbeat()

                _isRegistered.value = true

            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(
                    registrationStatus = RegistrationStatus.ERROR,
                    errorMessage = e.message ?: "Registration failed"
                )
            }
        }
    }

    private fun normalizeUrl(url: String): String {
        var normalized = url.trim()

        // Add protocol if missing
        if (!normalized.startsWith("http://") && !normalized.startsWith("https://")) {
            normalized = "http://$normalized"
        }

        // Add default port if missing
        val protocolEnd = normalized.indexOf("://") + 3
        val hostPart = normalized.substring(protocolEnd)
        if (!hostPart.contains(":") && !hostPart.contains("/")) {
            normalized = "$normalized:8420"
        } else if (hostPart.contains("/") && !hostPart.substringBefore("/").contains(":")) {
            val protocol = normalized.substring(0, protocolEnd)
            val rest = normalized.substring(protocolEnd)
            val host = rest.substringBefore("/")
            val path = rest.substringAfter("/", "")
            normalized = "$protocol$host:8420/$path"
        }

        return normalized.trimEnd('/')
    }
}

data class SetupUiState(
    val serverUrl: String = "",
    val connectionStatus: ConnectionStatus = ConnectionStatus.IDLE,
    val registrationStatus: RegistrationStatus = RegistrationStatus.IDLE,
    val serverVersion: String? = null,
    val clientId: String? = null,
    val errorMessage: String? = null
)

enum class ConnectionStatus {
    IDLE,
    TESTING,
    SUCCESS,
    ERROR
}

enum class RegistrationStatus {
    IDLE,
    REGISTERING,
    SUCCESS,
    ERROR
}
