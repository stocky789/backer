package com.backer.android.presentation.status

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.backer.android.data.repository.BackerApiRepository
import com.backer.android.data.repository.CredentialRepository
import com.backer.android.domain.model.ConnectionStatus
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import javax.inject.Inject

@HiltViewModel
class StatusViewModel @Inject constructor(
    private val credentialRepository: CredentialRepository,
    private val apiRepository: BackerApiRepository
) : ViewModel() {

    private val _uiState = MutableStateFlow(StatusUiState())
    val uiState: StateFlow<StatusUiState> = _uiState.asStateFlow()

    init {
        loadStatus()
    }

    private fun loadStatus() {
        val credentials = credentialRepository.getCredentials()
        val lastHeartbeat = credentialRepository.getLastHeartbeat()
        val lastBackup = credentialRepository.getLastBackup()

        _uiState.value = StatusUiState(
            clientId = credentials?.clientId,
            serverUrl = credentials?.serverUrl,
            lastHeartbeat = lastHeartbeat?.let { formatTimestamp(it) },
            lastBackup = lastBackup?.let { formatTimestamp(it) },
            connectionStatus = if (credentials != null) ConnectionStatus.CONNECTED else ConnectionStatus.DISCONNECTED
        )
    }

    fun refreshStatus() {
        _uiState.value = _uiState.value.copy(
            connectionStatus = ConnectionStatus.CONNECTING
        )

        viewModelScope.launch {
            val result = apiRepository.sendHeartbeat()
            result.fold(
                onSuccess = {
                    _uiState.value = _uiState.value.copy(
                        connectionStatus = ConnectionStatus.CONNECTED,
                        lastHeartbeat = formatTimestamp(System.currentTimeMillis())
                    )
                },
                onFailure = {
                    _uiState.value = _uiState.value.copy(
                        connectionStatus = ConnectionStatus.ERROR,
                        errorMessage = it.message
                    )
                }
            )
        }
    }

    private fun formatTimestamp(timestamp: Long): String {
        val formatter = SimpleDateFormat("MMM dd, HH:mm:ss", Locale.getDefault())
        return formatter.format(Date(timestamp))
    }
}

data class StatusUiState(
    val clientId: String? = null,
    val serverUrl: String? = null,
    val lastHeartbeat: String? = null,
    val lastBackup: String? = null,
    val connectionStatus: ConnectionStatus = ConnectionStatus.DISCONNECTED,
    val errorMessage: String? = null
)