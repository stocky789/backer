package com.backer.android.presentation.navigation

import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import com.backer.android.presentation.settings.SettingsScreen
import com.backer.android.presentation.setup.SetupScreen
import com.backer.android.presentation.setup.SetupViewModel
import com.backer.android.presentation.status.StatusScreen
import com.backer.android.presentation.status.StatusViewModel

sealed class Screen(val route: String) {
    data object Setup : Screen("setup")
    data object Status : Screen("status")
    data object Settings : Screen("settings")
}

@Composable
fun BackerNavHost() {
    val navController = rememberNavController()

    // Check if already registered to determine start destination
    val setupViewModel: SetupViewModel = hiltViewModel()
    val isRegistered by setupViewModel.isRegistered.collectAsState()

    val startDestination = if (isRegistered) Screen.Status.route else Screen.Setup.route

    NavHost(
        navController = navController,
        startDestination = startDestination
    ) {
        composable(Screen.Setup.route) {
            SetupScreen(
                viewModel = setupViewModel,
                onRegistrationSuccess = {
                    navController.navigate(Screen.Status.route) {
                        popUpTo(Screen.Setup.route) { inclusive = true }
                    }
                }
            )
        }

        composable(Screen.Status.route) {
            val statusViewModel: StatusViewModel = hiltViewModel()
            StatusScreen(
                viewModel = statusViewModel,
                onNavigateToSettings = {
                    navController.navigate(Screen.Settings.route)
                }
            )
        }

        composable(Screen.Settings.route) {
            SettingsScreen(
                onNavigateBack = {
                    navController.popBackStack()
                },
                onDisconnected = {
                    // Clear entire back stack and navigate to Setup
                    navController.navigate(Screen.Setup.route) {
                        popUpTo(navController.graph.id) { inclusive = true }
                        launchSingleTop = true
                    }
                }
            )
        }

    }
}
