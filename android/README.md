# Backer Android Client

Android backup agent for the Backer backup system.

## Features

- Register with Backer server as a backup agent
- Execute backups on-demand or scheduled
- Browse device filesystem for backup source selection
- Stream backups to server using the proxy backend
- Real-time progress reporting
- Background execution with WorkManager

## Repository formats and Android access

- Android always uses the proxy transport and is repository-format blind: the server stores the uploaded tar.gz in either an encrypted Kopia repository or an unencrypted, readable files repository.
- The proxy archive is plaintext transport data; use HTTPS in production. Files repositories leave names and contents readable to anyone with storage access.
- Enable **All files access** in the app’s Storage access status for unattended raw-path backup, browsing, and restore. Android cannot access another app’s protected data. Android Auto Backup and Storage Access Framework URI permissions are not used for raw-path jobs.
- Restore downloads the chosen immutable snapshot and extracts it with traversal protection; clean restore activates only after extraction succeeds.
- Android 15 limits data-sync foreground work. Large jobs can fail visibly and should be retried.
- There is no in-place repository conversion. Create a repository in the desired format and run a fresh backup.

## Requirements

- Android 8.0 (API 26) or higher
- Backer server v0.7.2 or higher

## Building

### Prerequisites

- Android Studio Ladybug (2024.2.1) or higher
- JDK 17

### Build Debug APK

```bash
cd android
./gradlew assembleDebug
```

The APK will be at `app/build/outputs/apk/debug/app-debug.apk`

### Build Release APK

```bash
cd android
./gradlew assembleRelease
```

## Architecture

- **Kotlin** with Coroutines for async operations
- **Jetpack Compose** for UI
- **Hilt** for dependency injection
- **Retrofit** + OkHttp for networking
- **WorkManager** for background tasks
- **EncryptedSharedPreferences** for secure credential storage

## Project Structure

```
app/src/main/java/com/backer/android/
├── BackerApplication.kt     # Application class
├── MainActivity.kt          # Main activity
├── di/                      # Hilt modules
├── data/
│   ├── api/                 # Retrofit API service
│   └── repository/          # Data repositories
├── domain/
│   ├── model/               # Domain models
│   └── usecase/             # Business logic
├── presentation/
│   ├── setup/               # Setup/registration screen
│   ├── status/              # Status screen
│   ├── navigation/          # Navigation
│   └── theme/               # Material 3 theme
├── worker/                  # WorkManager workers
├── service/                 # Foreground service
└── receiver/                # Boot receiver
```

## Usage

1. Install the APK on your Android device
2. Open the app and enter your Backer server URL
3. Tap "Test Connection" to verify connectivity
4. Tap "Register Agent" to register the device
5. The agent will appear in the Backer web UI under Agents
6. Create backup jobs targeting the Android agent

## Security

- Credentials stored using Android Keystore encryption
- HTTPS recommended for production servers
- Local network HTTP allowed for development

## License

MIT License - see main repository for details.
