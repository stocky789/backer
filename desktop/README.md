# Backer Desktop (Avalonia)

The one desktop GUI for Windows and Linux. Replaces the Tk agent GUI.

    dotnet build desktop/Backer.Desktop.sln -c Release
    dotnet test  desktop/Backer.Desktop.sln
    dotnet publish desktop/Backer.Desktop/Backer.Desktop.csproj -c Release -r win-x64   --self-contained -p:PublishSingleFile=true
    dotnet publish desktop/Backer.Desktop/Backer.Desktop.csproj -c Release -r linux-x64 --self-contained -p:PublishSingleFile=true

Contract (D8): the GUI reads `config.yaml` and `<data_dir>` directly and read-only,
and performs every mutation by spawning the `backer` CLI — secrets on stdin only, never
in argv, cancel = kill the child process tree. It never touches the OS keystore, never
invokes kopia, and never writes `config.yaml`. Failure text shown to the user is the
CLI's own output, verbatim.

Targets net8.0; `dotnet test` rolls forward to a newer installed runtime.

Repository format is separate from transport. The wizard defaults to encrypted,
versioned `kopia`; `files` stores browsable plaintext full snapshots on local or
SMB storage, skips passphrase/recovery steps, and does not support S3. Files mode
preserves regular-file contents plus basic timestamps/mode, but not ACLs, ADS,
xattrs, sparse files, hard-link identity, VSS, or crash consistency; symlinks and
unreadable files fail a snapshot. There is no in-place conversion: create a new
repository and run a fresh backup.

Commands the views spawn (the CLI owns the wording of every failure):

    backer job run NAME --json          run_id on the first stdout line, result JSON on the last
    backer job rm NAME --repo ID --yes  after the Remove-job modal
    backer snapshots JOB --json         restore snapshot list
    backer restore --job J --snapshot S --into MODE [--destination D] [--include P] --no-progress
                                        REPLACE adds --yes-replace, and is dry-run + typed-modal gated

    backer repo discover --host H --username U --password-stdin --json   wizard share listing
    backer repo add NAME --init|--attach --headless --type ...           wizard create step
    backer repo passphrase NAME --passphrase-out FILE   "Save recovery record" (wizard + Settings)
    backer repo rm NAME --yes --confirm-name NAME       after the typed-name modal
    backer job create NAME --source P [--schedule CRON|--no-schedule] [--keep-* N] [--exclude P]
    backer job set NAME [changed flags only]                             Home -> Edit
    backer schedule show|status --json, schedule pause [--until ISO]     settings + tray + status strip
    backer agent register|status|install|uninstall --mode server|local   settings
    backer agent uninstall --mode server --service-only --yes            "Remove agent service"
    backer agent test-schedule                                           "Test a scheduled run now"
    backer keystore status --json                                        file-fallback banner

"Remove agent service" is `--service-only`: the plain `agent uninstall --mode server --yes` deletes
the config and data directories, which is a full uninstall and not something a button may do.
`agent uninstall --mode local --yes` (turn off scheduled backups) removes only the task/timer — it
writes nothing to config.yaml and deletes nothing.

Pause durations (1 hour / until tomorrow / until turned back on) are ISO 8601 stamps computed in
C# and passed as `schedule pause --until`.

An `agent install`/`uninstall` refused for privilege is reported as "Restart Backer as administrator
to change service settings" with the CLI's own message kept underneath it — never as success.

Secrets: the passphrase goes on stdin. The CLI refuses two stdin secrets in one call, so an SMB
password, an S3 secret key and the enrolment token travel in the child process environment
(`BACKER_SMB_PASSWORD`, `BACKER_S3_SECRET_KEY`, `BACKER_ENROLLMENT_TOKEN`) — never argv, never a file.

Cron strings are not parsed here: the CLI validates them and its error is what the user reads.

The six confirmation sites, app-wide (`SettingsAndNotificationTests.ThereAreExactlySixConfirmationDialogs`
pins them by name, not by count):

1. `HomeViewModel.RemoveAsync` — remove a backup job
2. `RestoreViewModel.ConfirmReplaceAsync` — restore over the originals, typed `REPLACE`
3. `SettingsViewModel.RemoveRepositoryAsync` — remove a repository, typed repository name
4. `SettingsViewModel.DeleteRepositoryDataAsync` — permanently erase an SMB repository, typed `DELETE name`
5. `SettingsViewModel.ConfirmStopAsync` — stop something that runs backups by itself
6. `MainWindowViewModel.ConfirmInterruptAsync` — close the app down mid-run

Eight prompts fit in the six sites because two sites are parameterised. Site 5 serves "Turn off
scheduled backups" and "Remove agent service": neither deletes anything, both must say exactly what
stops running. Site 6 serves "Quit Backer" and "Install the latest Backer": both stop a backup that
may be in flight. Everything else asks in place, without a modal.

Tray (Windows and Linux): open, back up now per job, pause (1 hour / until tomorrow / until turned
back on) or resume, open failed runs, logs, settings, exit. Closing the window hides to the tray with
a one-time hint. Confirmations restore the window before they open — Avalonia refuses a dialog owned
by a hidden window, so a tray-triggered modal would otherwise fault silently.

Notification policy: a failure at most once per job per UTC day, the first success once per job, a
needs-input run once per run id — recorded in the GUI's own app data (`gui-state.json`), never in
the agent's data dir.

**Known gap — Windows desktop notifications.** Only Linux gets a real desktop notification
(`notify-send`). Windows toasts need a registered AppUserModelID, and the maintained cross-platform
option (`DesktopNotifications.Avalonia`) is pinned to Avalonia 0.10 and does not restore against
this app's Avalonia 12, so it was not adopted. On Windows the same message reaches the user through
the tray tooltip count and an in-app attention banner at the top of the window. Revisit when a
notification library that supports current Avalonia exists.

`Services/Cells.cs` repeats the platform/storage list from `backer.serverless.cells`; the
Python contract test is what catches a drift between the two.
