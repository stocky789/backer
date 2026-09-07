using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using Backer.Desktop.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Backer.Desktop.ViewModels;

/// <summary>One row of `backer snapshots JOB --json`.</summary>
public sealed class SnapshotRow
{
    public string? Id { get; set; }

    public string? FullId { get; set; }

    public string? Timestamp { get; set; }

    public List<string> Paths { get; set; } = new();

    public long Size { get; set; }

    public string Selector => FullId ?? Id ?? "";

    public string Path => Paths.Count > 0 ? Paths[0] : "";

    public string HumanSize => HomeViewModel.HumanSize(Size);
}

public sealed partial class RestoreViewModel : ViewModelBase
{
    private readonly AppServices _services;
    private readonly MainWindowViewModel? _shell;

    private CancellationTokenSource? _cancellation;

    public RestoreViewModel(AppServices services, MainWindowViewModel? shell = null)
    {
        _services = services;
        _shell = shell;
    }

    public override string Title => "Restore";

    public ObservableCollection<string> JobNames { get; } = new();

    public ObservableCollection<SnapshotRow> Snapshots { get; } = new();

    public IReadOnlyList<string> Modes { get; } = new[] { "NEW", "MERGE", "REPLACE" };

    [ObservableProperty]
    private string? _selectedJobName;

    [ObservableProperty]
    private SnapshotRow? _selectedSnapshot;

    [ObservableProperty]
    private string _mode = "NEW";

    [ObservableProperty]
    private string _destination = "";

    /// <summary>Folder inside the snapshot; empty restores everything.</summary>
    [ObservableProperty]
    private string _include = "";

    [ObservableProperty]
    private string _statusText = "Choose a local job.";

    [ObservableProperty]
    private bool _busy;

    [ObservableProperty]
    private string _detail = "";

    /// <summary>The last restore failed: the copy-error and open-log actions become available.</summary>
    [ObservableProperty]
    private bool _failed;

    /// <summary>
    /// The CLI refused a protected destination (system folder, home folder). The refusal names
    /// the override; this offers it inline so the user never needs a terminal.
    /// </summary>
    [ObservableProperty]
    private bool _protectedDestinationOffered;

    [ObservableProperty]
    private bool _restoreIntoProtectedFolder;

    /// <summary>The exact resolved path from the CLI's refusal, echoed back as the confirmation.</summary>
    private string? _protectedPath;

    partial void OnDestinationChanged(string value) => ResetProtectedDestination();

    partial void OnModeChanged(string value) => ResetProtectedDestination();

    private void ResetProtectedDestination()
    {
        ProtectedDestinationOffered = false;
        RestoreIntoProtectedFolder = false;
        _protectedPath = null;
    }

    /// <summary>Offer the inline override when the failure is the protected-destination refusal.</summary>
    public bool TryOfferProtectedDestination(string failureText)
    {
        var match = System.Text.RegularExpressions.Regex.Match(
            failureText, "--confirm-destination \"([^\"]+)\"");
        if (!match.Success)
        {
            return false;
        }
        _protectedPath = match.Groups[1].Value;
        ProtectedDestinationOffered = true;
        RestoreIntoProtectedFolder = false;
        return true;
    }

    public override IRelayCommand PrimaryCommand => RestoreCommand;

    public override void OnShown() => ReloadJobs();

    public void Start(string jobName)
    {
        ReloadJobs();
        SelectedJobName = jobName;
        _ = LoadSnapshotsAsync();
    }

    private void ReloadJobs()
    {
        var selected = SelectedJobName;
        JobNames.Clear();
        try
        {
            foreach (var name in _services.Config.Load().Jobs.Keys.OrderBy(name => name, StringComparer.OrdinalIgnoreCase))
            {
                JobNames.Add(name);
            }
        }
        catch (Exception error)
        {
            _services.Status.Set(error.Message, error: true);
        }
        SelectedJobName = JobNames.Contains(selected ?? "") ? selected : null;
    }

    [RelayCommand]
    public async Task LoadSnapshotsAsync()
    {
        if (SelectedJobName is not { } job)
        {
            return;
        }
        Snapshots.Clear();
        SelectedSnapshot = null;
        Busy = true;
        StatusText = "Checking the repository and loading snapshots…";
        var result = await _services.Cli.RunAsync(new[] { "snapshots", job, "--json" });
        Busy = false;
        if (!result.Ok)
        {
            StatusText = result.FailureText;
            _services.Status.Set(result.FailureText, error: true);
            return;
        }
        foreach (var row in result.Json<List<SnapshotRow>>() ?? new List<SnapshotRow>())
        {
            Snapshots.Add(row);
        }
        StatusText = Snapshots.Count > 0
            ? "Select a snapshot and a restore destination."
            : "No snapshots found; the repository was checked.";
    }

    [RelayCommand]
    public async Task ChooseDestinationAsync()
    {
        var folder = await _services.PickFolder();
        if (folder is not null)
        {
            Destination = folder;
        }
    }

    [RelayCommand]
    public async Task RestoreAsync()
    {
        if (SelectedJobName is not { } job || SelectedSnapshot is null)
        {
            StatusText = "Select a snapshot first.";
            return;
        }
        if (Destination.Trim().Length == 0 && Mode != "NEW")
        {
            StatusText = "Choose a restore destination.";
            return;
        }

        var arguments = BuildArguments(job, SelectedSnapshot.Selector);
        Busy = true;
        Failed = false;
        _cancellation = new CancellationTokenSource();
        try
        {
            if (Mode == "REPLACE" && !await ConfirmReplaceAsync(arguments))
            {
                StatusText = "Restore cancelled; the destination is unchanged.";
                return;
            }
            StatusText = "Restoring…";
            var result = await _services.Cli.RunAsync(arguments, cancellationToken: _cancellation.Token);
            if (result.Cancelled)
            {
                StatusText = "Restore cancelled";
                _services.Status.Set(StatusText, error: true);
            }
            else if (result.Ok)
            {
                Detail = result.Stdout.Trim();
                StatusText = "Restore completed";
                _services.Status.Set(StatusText);
            }
            else
            {
                Detail = result.FailureText;
                Failed = true;
                _services.Status.Set(result.FailureText, error: true);
                StatusText = TryOfferProtectedDestination(result.FailureText)
                    ? "That folder is protected. Tick the confirmation below to restore into it anyway."
                    : result.FailureText;
            }
        }
        finally
        {
            Busy = false;
        }
    }

    /// <summary>
    /// REPLACE moves whatever is in the destination aside, so it is shown as a dry run first
    /// and then gated on a typed confirmation. No restore runs without both.
    /// </summary>
    private async Task<bool> ConfirmReplaceAsync(IReadOnlyList<string> arguments)
    {
        StatusText = "Checking what would be replaced…";
        var preview = await _services.Cli.RunAsync(
            arguments.Concat(new[] { "--dry-run" }), cancellationToken: _cancellation!.Token);
        if (!preview.Ok)
        {
            Detail = preview.FailureText;
            _services.Status.Set(preview.FailureText, error: true);
            StatusText = TryOfferProtectedDestination(preview.FailureText)
                ? "That folder is protected. Tick the confirmation below to restore into it anyway."
                : preview.FailureText;
            return false;
        }
        Detail = preview.Stdout.Trim();
        return await _services.Confirm(new ConfirmRequest(
            "Overwrite the original folder",
            preview.Stdout.Trim() + "\n\nWhat is in that folder now is moved aside first, then the snapshot is restored over it.",
            "Restore over originals",
            TypedConfirmation: "REPLACE"));
    }

    public IReadOnlyList<string> BuildArguments(string job, string snapshot)
    {
        var arguments = new List<string>
        {
            "restore", "--job", job, "--snapshot", snapshot, "--into", Mode, "--no-progress",
        };
        if (Destination.Trim().Length > 0)
        {
            arguments.Add("--destination");
            arguments.Add(Destination.Trim());
        }
        if (Include.Trim().Length > 0)
        {
            arguments.Add("--include");
            arguments.Add(Include.Trim());
        }
        if (Mode == "REPLACE")
        {
            // The flag carries the confirmation the GUI already collected (non-TTY child process).
            arguments.Add("--yes-replace");
        }
        if (RestoreIntoProtectedFolder && _protectedPath is not null)
        {
            // Echoes the CLI's own resolved path back, standing in for the typed confirmation.
            arguments.Add("--confirm-destination");
            arguments.Add(_protectedPath);
        }
        return arguments;
    }

    [RelayCommand]
    public void Stop()
    {
        _cancellation?.Cancel();
        StatusText = CliRunner.StopWording("restore");
        _services.Status.Set(StatusText, error: true);
    }

    /// <summary>After a failure: the CLI's own error text, for pasting into a bug report.</summary>
    [RelayCommand]
    public Task CopyErrorAsync() => _services.CopyText(Detail.Length > 0 ? Detail : StatusText);

    [RelayCommand]
    public void OpenLogFolder()
    {
        var directory = System.IO.Path.Combine(_services.Data.DataDir, "logs");
        _services.OpenFolder(directory);
        _services.Status.Set($"Logs: {directory}");
    }

    [RelayCommand]
    private void Back() => _shell?.GoHome();
}
