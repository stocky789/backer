using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Text.Json.Serialization;
using System.Threading.Tasks;
using Backer.Desktop.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Backer.Desktop.ViewModels;

/// <summary>`backer schedule status --json`.</summary>
public sealed class ScheduleStatus
{
    public bool Configured { get; set; }

    public string? Platform { get; set; }

    public string? Method { get; set; }

    public string? Scope { get; set; }

    public bool Enabled { get; set; }

    public bool Active { get; set; }
}

/// <summary>`backer schedule show --json`.</summary>
public sealed class SchedulePause
{
    public bool Paused { get; set; }

    public string? Until { get; set; }
}

/// <summary>`backer keystore status --json`.</summary>
public sealed class KeystoreStatus
{
    public string? Backend { get; set; }

    [JsonPropertyName("file_fallback")]
    public bool FileFallback { get; set; }
}

/// <summary>One row of the Settings repository list.</summary>
public sealed class RepositoryRow
{
    public required string Name { get; init; }

    public required string Id { get; init; }

    public required string Type { get; init; }

    public required string Format { get; init; }

    public bool IsEncrypted => Format == "kopia";

    public required string Detail { get; init; }

    public override string ToString() => $"{Name} — {Detail}";
}

public sealed partial class SettingsViewModel : ViewModelBase
{
    /// <summary>Same release the Inno installer publishes to (features.md §10).</summary>
    public const string InstallerUrl =
        "https://git.stockhome.com.au/stocky789/backer/releases/download/release-main/backer-agent-setup.exe";

    public const string LinuxUpdateCommand =
        "pip install --upgrade git+https://git.stockhome.com.au/stocky789/backer.git@main";

    /// <summary>ERROR_CANCELLED: the user said No to the installer's UAC prompt.</summary>
    private const int ErrorCancelled = 1223;

    /// <summary>Declining UAC installs nothing and changes nothing — a statement, not an error.</summary>
    public const string UpdateDeclined = "The update was not installed.";

    /// <summary>
    /// UseShellExecute is required: the installer's manifest asks for administrator and we run
    /// asInvoker, so a plain CreateProcess can only ever fail with 740 (elevation required).
    /// ShellExecute raises the UAC prompt instead. ArgumentList is not supported with it, so the
    /// Inno Setup unattended flags go in Arguments (same flags as the Tk agent used).
    /// </summary>
    public static ProcessStartInfo InstallerStartInfo(string installer) => new(installer)
    {
        Arguments = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART",
        UseShellExecute = true,
    };

    private readonly AppServices _services;
    private readonly MainWindowViewModel? _shell;

    public SettingsViewModel(AppServices services, MainWindowViewModel? shell = null)
    {
        _services = services;
        _shell = shell;
        Theme = services.StateStore.Load().Theme;
    }

    public SettingsViewModel()
        : this(new AppServices())
    {
    }

    public override string Title => "Settings";

    public override IRelayCommand PrimaryCommand => ConnectCommand;

    public IReadOnlyList<string> Themes { get; } = new[] { "system", "light", "dark" };

    public bool IsWindows => OperatingSystem.IsWindows();

    [ObservableProperty]
    private string _serverUrl = "";

    [ObservableProperty]
    private string _enrollmentToken = "";

    [ObservableProperty]
    private string _statusText = "";

    [ObservableProperty]
    private bool _busy;

    [ObservableProperty]
    private string _scheduleText = "Local schedule: unknown";

    [ObservableProperty]
    private bool _scheduleConfigured;

    [ObservableProperty]
    private bool _paused;

    [ObservableProperty]
    private string _keystoreWarning = "";

    public bool KeystoreFallback => KeystoreWarning.Length > 0;

    [ObservableProperty]
    private string _theme = "system";

    public ObservableCollection<RepositoryRow> Repositories { get; } = new();

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(HasRepositorySelection))]
    [NotifyPropertyChangedFor(nameof(CanDeleteRepositoryData))]
    [NotifyPropertyChangedFor(nameof(CanSaveRecoveryRecord), nameof(IsEncryptedRepository))]
    private RepositoryRow? _selectedRepository;

    public bool HasRepositorySelection => SelectedRepository is not null;

    public bool CanDeleteRepositoryData => SelectedRepository?.Type == "smb";

    public bool IsEncryptedRepository => SelectedRepository?.IsEncrypted == true;

    /// <summary>The recovery record is written in plain text; the user says so before it is.</summary>
    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(CanSaveRecoveryRecord))]
    private bool _plainTextAck;

    public bool CanSaveRecoveryRecord => PlainTextAck && IsEncryptedRepository;

    public static string PlainTextAcknowledgement => RecoveryRecord.Acknowledgement;

    /// <summary>Verbatim output of the last `agent test-schedule`.</summary>
    [ObservableProperty]
    private string _scheduledTestOutput = "";

    [ObservableProperty]
    private bool _scheduledTestRunning;

    public string UpdateHint => IsWindows
        ? "Downloads and runs the latest installer."
        : $"Update with: {LinuxUpdateCommand}";

    partial void OnKeystoreWarningChanged(string value) => OnPropertyChanged(nameof(KeystoreFallback));

    partial void OnThemeChanged(string value)
    {
        var state = _services.StateStore.Load();
        state.Theme = value;
        _services.StateStore.Save(state);
        ThemeChanged?.Invoke(value);
    }

    /// <summary>App wires this to the Avalonia theme variant.</summary>
    public Action<string>? ThemeChanged { get; set; }

    public override void OnShown()
    {
        try
        {
            var config = _services.Config.Load();
            ServerUrl = config.Server?.ServerUrl ?? ServerUrl;
            LoadRepositories(config);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            // A missing config is a fresh install, not an error.
        }
        _ = RefreshAsync();
    }

    /// <summary>Repositories as configured; the GUI reads config.yaml, it never writes it.</summary>
    public void LoadRepositories(BackerConfig config)
    {
        var selected = SelectedRepository?.Id;
        Repositories.Clear();
        foreach (var (id, repository) in config.Repositories.OrderBy(item => item.Value.Name ?? item.Key, StringComparer.OrdinalIgnoreCase))
        {
            Repositories.Add(new RepositoryRow
            {
                Name = repository.Name ?? id,
                Id = repository.Id ?? id,
                Type = repository.Type ?? "",
                Format = repository.Format,
                Detail = string.Join(" · ", new[]
                {
                    repository.Format == "files" ? "unencrypted files" : "encrypted",
                    repository.Type ?? "",
                    repository.Path ?? repository.Bucket ?? repository.Share ?? "",
                }.Where(part => part.Length > 0)),
            });
        }
        SelectedRepository = Repositories.FirstOrDefault(row => row.Id == selected);
    }

    [RelayCommand]
    public async Task RefreshAsync()
    {
        await RefreshScheduleAsync();
        await RefreshPauseAsync();
        await RefreshKeystoreAsync();
    }

    public async Task RefreshScheduleAsync()
    {
        var result = await _services.Cli.RunAsync(new[] { "schedule", "status", "--json" });
        if (!result.Ok)
        {
            ScheduleText = result.FailureText;
            return;
        }
        var status = result.Json<ScheduleStatus>();
        ScheduleConfigured = status?.Configured ?? false;
        ScheduleText = status is null
            ? "Local schedule: unknown"
            : $"Local schedule: {(status.Configured ? "installed" : "not installed")} · {status.Method} · {status.Scope}"
              + $" · {(status.Enabled ? "enabled" : "disabled")} · {(status.Active ? "active" : "idle")}";
    }

    public async Task RefreshPauseAsync()
    {
        var result = await _services.Cli.RunAsync(new[] { "schedule", "show", "--json" });
        if (!result.Ok)
        {
            _services.Status.PauseState = "Pause state unknown";
            return;
        }
        var show = result.Json<SchedulePause>();
        Paused = show?.Paused ?? false;
        _services.Status.PauseState = PauseLabel(show);
    }

    /// <summary>The strip wording for a `schedule show --json` payload.</summary>
    public static string PauseLabel(SchedulePause? show) => show is null
        ? "Pause state unknown"
        : !show.Paused
            ? ""
            : string.IsNullOrEmpty(show.Until) ? "Paused" : $"Paused until {show.Until}";

    public async Task RefreshKeystoreAsync()
    {
        var result = await _services.Cli.RunAsync(new[] { "keystore", "status", "--json" });
        var status = result.Ok ? result.Json<KeystoreStatus>() : null;
        KeystoreWarning = status is { FileFallback: true }
            ? $"This computer has no OS keystore, so repository secrets are kept in protected local files ({status.Backend})."
            : "";
    }

    // ---- server -------------------------------------------------------------

    [RelayCommand]
    public async Task ConnectAsync()
    {
        if (ServerUrl.Trim().Length == 0)
        {
            StatusText = "Enter a server address.";
            return;
        }
        // The enrollment token is a secret: it goes in the child environment, never argv.
        await RunAsync(
            new[] { "agent", "register", "--server", ServerUrl.Trim() },
            environment: EnrollmentToken.Trim().Length > 0
                ? new Dictionary<string, string> { ["BACKER_ENROLLMENT_TOKEN"] = EnrollmentToken.Trim() }
                : null);
        EnrollmentToken = "";
    }

    [RelayCommand]
    public Task CheckStatusAsync() => RunAsync(new[] { "agent", "status" });

    [RelayCommand]
    public Task InstallServerServiceAsync() =>
        RunAsync(new[] { "agent", "install", "--mode", "server" }, serviceAction: true);

    /// <summary>
    /// On Linux `agent uninstall --mode server --yes` on its own also deletes the config and data
    /// directories; --service-only is what stops that. On Windows both forms only remove the
    /// service, so the flag is a no-op there. The GUI only ever removes the service on either
    /// platform, which is what the confirmation says — true on both.
    /// </summary>
    public static IReadOnlyList<string> RemoveServiceArguments() =>
        new[] { "agent", "uninstall", "--mode", "server", "--service-only", "--yes" };

    [RelayCommand]
    public async Task UninstallServerServiceAsync()
    {
        var confirmed = await ConfirmStopAsync(
            "Remove agent service",
            "The installed Backer service is removed from this computer, so it stops connecting to the "
            + "server. Your repositories, backup jobs, saved passphrases, run history and the Backer "
            + "program itself are all left exactly as they are.",
            "Remove the service");
        if (!confirmed)
        {
            StatusText = "The agent service was left installed.";
            return;
        }
        await RunAsync(RemoveServiceArguments(), serviceAction: true);
    }

    /// <summary>
    /// Confirmation site 4 of 5: switching off something that runs backups by itself. Both
    /// prompts that reach it (turn off scheduled backups, remove the agent service) delete no
    /// data, and both must say exactly what stops.
    /// </summary>
    private Task<bool> ConfirmStopAsync(string title, string body, string confirmLabel) =>
        _services.Confirm(new ConfirmRequest(title, body, confirmLabel));

    // ---- local scheduling ---------------------------------------------------

    /// <summary>
    /// `agent install --mode local` refuses the default method on both platforms, so the
    /// per-platform one is always explicit (cli.py: task on Windows, systemd on Linux).
    /// </summary>
    public static IReadOnlyList<string> EnableScheduleArguments() => new[]
    {
        "agent", "install", "--mode", "local", "--method", OperatingSystem.IsWindows() ? "task" : "systemd",
    };

    [RelayCommand]
    public async Task EnableScheduleAsync()
    {
        await RunAsync(EnableScheduleArguments(), serviceAction: true);
        await RefreshScheduleAsync();
    }

    [RelayCommand]
    public async Task DisableScheduleAsync()
    {
        var confirmed = await ConfirmStopAsync(
            "Turn off scheduled backups",
            "The scheduled trigger is removed from this computer. Nothing already backed up is deleted, "
            + "but no backup will run again until you turn this back on.",
            "Turn off");
        if (!confirmed)
        {
            StatusText = "Scheduled backups were left on.";
            return;
        }
        // Verified against src/backer/cli.py `agent uninstall --mode local`: it calls only
        // remove_local_scheduled_task() / remove_local_systemd_timer() and returns. It touches
        // neither config.yaml nor the data dir, so the honest copy above is the whole story.
        await RunAsync(new[] { "agent", "uninstall", "--mode", "local", "--yes" }, serviceAction: true);
        await RefreshScheduleAsync();
    }

    /// <summary>
    /// `agent test-schedule` runs the job the way the scheduler would (a transient SYSTEM task
    /// or systemd unit) and cleans up fail-closed. Its output is shown verbatim.
    /// </summary>
    [RelayCommand]
    public async Task TestScheduledRunAsync()
    {
        if (ScheduledTestRunning)
        {
            return;
        }
        ScheduledTestRunning = true;
        ScheduledTestOutput = "Running one scheduled backup now…";
        try
        {
            var result = await _services.Cli.RunAsync(new[] { "agent", "test-schedule" });
            var output = result.Ok ? result.Stdout.Trim() : result.FailureText;
            ScheduledTestOutput = output.Length > 0 ? output : $"backer exited with code {result.ExitCode}";
            _services.Status.Set(ScheduledTestOutput, error: !result.Ok);
        }
        finally
        {
            ScheduledTestRunning = false;
        }
    }

    // ---- pause --------------------------------------------------------------

    /// <summary>ISO 8601 with the local offset — `schedule pause --until` parses exactly this.</summary>
    public static string PauseStamp(DateTimeOffset moment) =>
        moment.ToString("yyyy-MM-ddTHH:mm:sszzz", CultureInfo.InvariantCulture);

    /// <summary>Local midnight at the start of the next day.</summary>
    public static string NextMidnightStamp(DateTimeOffset now) =>
        PauseStamp(new DateTimeOffset(now.Date.AddDays(1), now.Offset));

    public static IReadOnlyList<string> PauseArguments(string? until) => until is null
        ? new[] { "schedule", "pause" }
        : new[] { "schedule", "pause", "--until", until };

    [RelayCommand]
    public async Task PauseAsync()
    {
        await RunAsync(PauseArguments(null));
        await RefreshPauseAsync();
    }

    [RelayCommand]
    public async Task PauseOneHourAsync()
    {
        await RunAsync(PauseArguments(PauseStamp(DateTimeOffset.Now.AddHours(1))));
        await RefreshPauseAsync();
    }

    [RelayCommand]
    public async Task PauseUntilTomorrowAsync()
    {
        await RunAsync(PauseArguments(NextMidnightStamp(DateTimeOffset.Now)));
        await RefreshPauseAsync();
    }

    [RelayCommand]
    public async Task ResumeAsync()
    {
        await RunAsync(new[] { "schedule", "resume" });
        await RefreshPauseAsync();
    }

    // ---- repositories -------------------------------------------------------

    /// <summary>
    /// Writes the passphrase to a file the user chooses, through the CLI (the GUI never touches
    /// the keystore). Gated on the plain-text acknowledgement.
    /// </summary>
    [RelayCommand]
    public async Task SaveRecoveryRecordAsync()
    {
        if (SelectedRepository is not { } repository)
        {
            return;
        }
        if (!PlainTextAck)
        {
            StatusText = "Tick the acknowledgement first: the recovery record is plain text.";
            return;
        }
        if (!repository.IsEncrypted)
        {
            StatusText = "Unencrypted files repositories have no recovery passphrase.";
            return;
        }
        var folder = await _services.PickFolder();
        if (folder is null)
        {
            StatusText = "Nothing was written.";
            return;
        }
        var destination = RecoveryRecord.Destination(folder, repository.Name);
        await RunAsync(RecoveryRecord.Arguments(repository.Name, destination));
    }

    /// <summary>
    /// Confirmation site 3 of 5. The typed repository name collected here is passed on as
    /// `--confirm-name`, which is what stands in for the CLI's own interactive prompt.
    /// </summary>
    [RelayCommand]
    public async Task RemoveRepositoryAsync()
    {
        if (SelectedRepository is not { } repository)
        {
            return;
        }
        var recoveryWarning = repository.IsEncrypted
            ? "along with its saved passphrase, and every backup job that uses it stops. The snapshots already in the storage stay where they are, but without the passphrase nothing in them can be restored."
            : "and every backup job that uses it stops. The snapshots already in the storage stay where they are and remain readable to anyone with storage access.";
        var confirmed = await _services.Confirm(new ConfirmRequest(
            "Remove repository",
            $"'{repository.Name}' is removed from this computer {recoveryWarning}",
            "Remove repository",
            TypedConfirmation: repository.Name));
        if (!confirmed)
        {
            StatusText = "Nothing was removed.";
            return;
        }
        await RunAsync(new[] { "repo", "rm", repository.Name, "--yes", "--confirm-name", repository.Name });
        try
        {
            LoadRepositories(_services.Config.Load());
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            // The status strip already carries the CLI's own wording.
        }
    }

    [RelayCommand]
    public async Task DeleteRepositoryDataAsync()
    {
        if (SelectedRepository is not { } repository || repository.Type != "smb")
        {
            return;
        }
        var typed = $"DELETE {repository.Name}";
        var confirmed = await _services.Confirm(new ConfirmRequest(
            "Permanently delete repository",
            $"Every backup in '{repository.Name}' and its SMB repository folder will be permanently deleted. "
            + "Its backup jobs, saved credentials, and local repository entry are removed only after storage deletion succeeds. "
            + "A network failure may leave a partially deleted repository that Backer will not remove locally.",
            "Delete backups permanently",
            TypedConfirmation: typed));
        if (!confirmed)
        {
            StatusText = "Nothing was deleted.";
            return;
        }
        await RunAsync(new[] { "repo", "destroy", repository.Name, "--yes", "--confirm-name", typed });
        try
        {
            LoadRepositories(_services.Config.Load());
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            // The status strip already carries the CLI's own wording.
        }
    }

    // ---- logs and updates ---------------------------------------------------

    [RelayCommand]
    private void OpenLogs()
    {
        var directory = System.IO.Path.Combine(_services.Data.DataDir, "logs");
        _services.OpenFolder(directory);
        StatusText = $"Logs: {directory}";
    }

    [RelayCommand]
    public async Task CheckForUpdatesAsync()
    {
        if (!IsWindows)
        {
            StatusText = $"Update this agent with: {LinuxUpdateCommand}";
            return;
        }
        // Fail closed: with no shell there is nothing to show the modal on, so nothing runs.
        var confirmed = _shell is not null && await _shell.ConfirmInterruptAsync(
            "Install the latest Backer",
            "This downloads the current installer and runs it silently. Backer closes while it installs, "
            + "so a backup running now would be stopped.",
            "Download and install");
        if (!confirmed)
        {
            StatusText = "Nothing was downloaded.";
            return;
        }
        Busy = true;
        try
        {
            var destination = System.IO.Path.Combine(
                System.IO.Path.GetTempPath(), "backer-agent-setup.exe");
            using var client = new HttpClient();
            await using (var stream = await client.GetStreamAsync(InstallerUrl))
            await using (var file = File.Create(destination))
            {
                await stream.CopyToAsync(file);
            }
            using var process = Process.Start(InstallerStartInfo(destination));
            StatusText = "Installer started; restart Backer when it completes.";
        }
        catch (System.ComponentModel.Win32Exception error) when (error.NativeErrorCode == ErrorCancelled)
        {
            // The user declined the UAC prompt. Nothing ran, nothing changed: not an error.
            StatusText = UpdateDeclined;
        }
        catch (Exception error) when (error is HttpRequestException or IOException
            or UnauthorizedAccessException or System.ComponentModel.Win32Exception)
        {
            StatusText = error.Message;
            _services.Status.Set(error.Message, error: true);
        }
        finally
        {
            Busy = false;
        }
    }

    [RelayCommand]
    private void Back() => _shell?.GoHome();

    /// <summary>Shown instead of a raw errno when a service change was refused for privilege.</summary>
    public const string ElevationHint = "Restart Backer as administrator to change service settings.";

    /// <summary>
    /// Does this CLI failure mean "you are not elevated"? Windows says "Access is denied", the
    /// service APIs say "administrator", Linux says "Permission denied"/"Errno 13".
    /// </summary>
    public static bool NeedsElevation(string failureText)
    {
        var text = failureText.ToLowerInvariant();
        return text.Contains("access is denied")
            || text.Contains("permission denied")
            || text.Contains("errno 13")
            || text.Contains("administrator")
            || text.Contains("elevat")
            || text.Contains("must be run as root")
            || text.Contains("operation not permitted");
    }

    private async Task RunAsync(
        IEnumerable<string> arguments,
        IReadOnlyDictionary<string, string>? environment = null,
        bool serviceAction = false)
    {
        Busy = true;
        var result = await _services.Cli.RunAsync(arguments, environment: environment);
        Busy = false;
        if (result.Ok)
        {
            StatusText = result.Stdout.Trim();
        }
        else
        {
            // The CLI's own wording is kept verbatim; the hint is added above it, never instead.
            StatusText = serviceAction && NeedsElevation(result.FailureText)
                ? ElevationHint + "\n" + result.FailureText
                : result.FailureText;
        }
        _services.Status.Set(StatusText, error: !result.Ok);
    }
}
