package com.backer.android.presentation.settings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.backer.android.data.repository.CredentialRepository
import com.backer.android.worker.WorkManagerScheduler
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

@HiltViewModel
class SettingsViewModel @Inject constructor(
    private val credentialRepository: CredentialRepository,
    private val workManagerScheduler: WorkManagerScheduler
) : ViewModel() {

    private val _uiState = MutableStateFlow(SettingsUiState())
    val uiState: StateFlow<SettingsUiState> = _uiState.asStateFlow()

    init {
        loadSettings()
    }

    private fun loadSettings() {
        val credentials = credentialRepository.getCredentials()
        _uiState.value = SettingsUiState(
            serverUrl = credentials?.serverUrl ?: "",
            clientId = credentials?.clientId ?: "",
            isRegistered = credentialRepository.isRegistered()
        )
    }

    fun disconnect() {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isDisconnecting = true)

            try {
                // Stop heartbeat worker
                workManagerScheduler.stopHeartbeat()

                // Clear stored credentials
                credentialRepository.clearCredentials()

                _uiState.value = SettingsUiState(
                    isRegistered = false,
                    disconnectSuccess = true
                )
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(
                    isDisconnecting = false,
                    errorMessage = e.message ?: "Failed to disconnect"
                )
            }
        }
    }

    fun clearError() {
        _uiState.value = _uiState.value.copy(errorMessage = null)
    }

    fun clearDisconnectSuccess() {
        _uiState.value = _uiState.value.copy(disconnectSuccess = false)
    }
}

data class SettingsUiState(
    val serverUrl: String = "",
    val clientId: String = "",
    val isRegistered: Boolean = false,
    val isDisconnecting: Boolean = false,
    val disconnectSuccess: Boolean = false,
    val errorMessage: String? = null
)