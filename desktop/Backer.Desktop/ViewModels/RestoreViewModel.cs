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

    public string DisplayTime => DateTimeOffset.TryParse(Timestamp, out var time)
        ? time.ToLocalTime().ToString("d MMM yyyy, h:mm tt") : Timestamp ?? "Unknown date";
}

public sealed partial class RestoreViewModel : ViewModelBase
{
    private readonly AppServices _services;
    private readonly MainWindowViewModel? _shell;

    private CancellationTokenSource? _cancellation;
    private int _snapshotGeneration;

    public RestoreViewModel(AppServices services, MainWindowViewModel? shell = null)
    {
        _services = services;
        _shell = shell;
    }

    public override string Title => "Restore";

    public ObservableCollection<string> JobNames { get; } = new();

    public ObservableCollection<SnapshotRow> Snapshots { get; } = new();

    [ObservableProperty]
    private string? _selectedJobName;

    [ObservableProperty]
    private SnapshotRow? _selectedSnapshot;

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(RestoreOriginal), nameof(RestoreAction))]
    private bool _saveAsZip;

    public bool RestoreOriginal
    {
        get => !SaveAsZip;
        set => SaveAsZip = !value;
    }

    public string RestoreAction => SaveAsZip ? "Save ZIP" : "Restore files";

    [ObservableProperty]
    private string _originalLocation = "";

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(HasRestoredPath))]
    private string _restoredPath = "";

    public bool HasRestoredPath => RestoredPath.Length > 0;

    [ObservableProperty]
    private string _destination = "";

    [ObservableProperty]
    private string _statusText = "Choose a local job.";

    [ObservableProperty]
    private bool _busy;

    [ObservableProperty]
    private bool _restoring;

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(HasDetails))]
    private string _detail = "";

    public bool HasDetails => Detail.Length > 0;

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

    partial void OnSaveAsZipChanged(bool value) => ResetProtectedDestination();

    partial void OnSelectedJobNameChanged(string? value)
    {
        ResetProtectedDestination();
        RestoredPath = "";
        OriginalLocation = "";
        if (value is not null)
        {
            try
            {
                if (_services.Config.Load().Jobs.TryGetValue(value, out var job))
                {
                    OriginalLocation = job.Source?.Path ?? "";
                }
            }
            catch (Exception error)
            {
                _services.Status.Set(error.Message, error: true);
            }
        }
        _ = LoadSnapshotsAsync();
    }

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
        if (SelectedJobName == jobName)
        {
            if (!Busy)
            {
                _ = LoadSnapshotsAsync();
            }
        }
        else
        {
            SelectedJobName = jobName;
        }
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
        SelectedJobName = JobNames.Contains(selected ?? "") ? selected
            : JobNames.Count == 1 ? JobNames[0] : null;
    }

    [RelayCommand]
    public async Task LoadSnapshotsAsync()
    {
        var generation = ++_snapshotGeneration;
        Snapshots.Clear();
        SelectedSnapshot = null;
        if (SelectedJobName is not { } job)
        {
            Busy = false;
            return;
        }
        Busy = true;
        StatusText = "Checking the repository and loading snapshots…";
        var result = await _services.Cli.RunAsync(new[] { "snapshots", job, "--json" });
        if (generation != _snapshotGeneration)
        {
            return;
        }
        Busy = false;
        if (!result.Ok)
        {
            Detail = result.FailureText;
            Failed = true;
            StatusText = "Could not load backups. See Details for the error.";
            _services.Status.Set(StatusText, error: true);
            return;
        }
        foreach (var row in (result.Json<List<SnapshotRow>>() ?? new List<SnapshotRow>())
            .OrderByDescending(row => DateTimeOffset.TryParse(row.Timestamp, out var time) ? time : DateTimeOffset.MinValue))
        {
            Snapshots.Add(row);
        }
        SelectedSnapshot = Snapshots.FirstOrDefault();
        StatusText = Snapshots.Count > 0
            ? "Latest backup selected. Choose an earlier backup if needed."
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
        if (Busy)
        {
            return;
        }
        if (SelectedJobName is not { } job || SelectedSnapshot is null)
        {
            StatusText = "Select a snapshot first.";
            return;
        }
        if (SaveAsZip && Destination.Trim().Length == 0)
        {
            StatusText = "Choose a folder for the ZIP file.";
            return;
        }

        var arguments = BuildArguments(job, SelectedSnapshot.Selector);
        Busy = true;
        Restoring = true;
        Failed = false;
        Detail = "";
        RestoredPath = "";
        _cancellation = new CancellationTokenSource();
        try
        {
            StatusText = SaveAsZip ? "Preparing ZIP file…" : "Restoring files to their original location…";
            var result = await _services.Cli.RunAsync(arguments, cancellationToken: _cancellation.Token);
            if (result.Cancelled)
            {
                StatusText = "Restore cancelled";
                _services.Status.Set(StatusText, error: true);
            }
            else if (result.Ok)
            {
                Detail = result.Stdout.Trim();
                const string completed = "Restore completed to ";
                StatusText = Detail.Split('\n').Select(line => line.Trim())
                    .LastOrDefault(line => line.StartsWith(completed, StringComparison.Ordinal)) ?? "Restore completed";
                RestoredPath = StatusText.StartsWith(completed, StringComparison.Ordinal)
                    ? StatusText[completed.Length..] : "";
                _services.Status.Set(StatusText);
            }
            else
            {
                Detail = result.FailureText;
                Failed = true;
                StatusText = TryOfferProtectedDestination(result.FailureText)
                    ? "That folder is protected. Tick the confirmation below to restore into it anyway."
                    : "Restore failed. See Details for the error.";
                _services.Status.Set(StatusText, error: true);
            }
        }
        finally
        {
            Busy = false;
            Restoring = false;
            _cancellation.Dispose();
            _cancellation = null;
        }
    }

    public IReadOnlyList<string> BuildArguments(string job, string snapshot)
    {
        var arguments = new List<string>
        {
            "restore", "--job", job, "--snapshot", snapshot, "--into", SaveAsZip ? "ZIP" : "ORIGINAL", "--no-progress",
        };
        if (SaveAsZip && Destination.Trim().Length > 0)
        {
            arguments.Add("--destination");
            arguments.Add(Destination.Trim());
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
    public void OpenRestoredLocation()
    {
        if (HasRestoredPath)
        {
            var folder = System.IO.File.Exists(RestoredPath)
                ? System.IO.Path.GetDirectoryName(RestoredPath) : RestoredPath;
            if (folder is not null)
            {
                _services.OpenFolder(folder);
            }
        }
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
