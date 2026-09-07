using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Backer.Desktop.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Backer.Desktop.ViewModels;

public sealed partial class RunViewModel : ViewModelBase
{
    private readonly AppServices _services;
    private readonly List<string> _stdout = new();

    private CancellationTokenSource? _cancellation;
    private bool _running;
    private bool _finished = true;
    private long _mark = -1;
    private DateTimeOffset _marked;
    // For the throughput estimate: bytes/time at the last frame that moved.
    private long _rateBytes = -1;
    private DateTimeOffset _rateAt;
    private double _bytesPerSecond;

    public RunViewModel(AppServices services) => _services = services;

    internal static string HumanBytes(long count)
    {
        double size = count;
        string[] units = { "B", "KB", "MB", "GB", "TB" };
        var unit = 0;
        while (size >= 1024 && unit < units.Length - 1)
        {
            size /= 1024;
            unit++;
        }
        return unit == 0 ? $"{count} B" : $"{size:0.0} {units[unit]}";
    }

    /// <summary>
    /// Once bytes are flowing, this much silence earns a gentle "still connected" note. It is
    /// long on purpose: a slow network share can be quiet for a while between frames, and the
    /// initial scan (no bytes yet) is never treated as a stall at all.
    /// </summary>
    public static readonly TimeSpan StallAfter = TimeSpan.FromSeconds(90);

    /// <summary>Replaced in tests so the stall rule can be exercised without waiting.</summary>
    public Func<DateTimeOffset> Clock { get; set; } = () => DateTimeOffset.UtcNow;

    public override string Title => "Backup run";

    [ObservableProperty]
    private string _jobName = "";

    [ObservableProperty]
    private string _state = "Waiting";

    [ObservableProperty]
    private bool _indeterminate = true;

    [ObservableProperty]
    private double _progressValue;

    [ObservableProperty]
    private double _progressMaximum = 1;

    [ObservableProperty]
    private string _logText = "";

    /// <summary>Stop stays disabled until the CLI has told us the run started.</summary>
    [ObservableProperty]
    private bool _canStop;

    /// <summary>The last run failed: the copy-error and open-log actions become available.</summary>
    [ObservableProperty]
    private bool _failed;

    /// <summary>The run finished cleanly — the state line turns Success.</summary>
    [ObservableProperty]
    private bool _succeeded;

    /// <summary>No new progress for StallAfter — the state line turns Warning.</summary>
    [ObservableProperty]
    private bool _stalled;

    public bool IsRunning => _running;

    public string? RunId { get; private set; }

    /// <summary>`{"run_id": "..."}` — the first stdout line of `job run --json`.</summary>
    public static string? TryRunId(string line)
    {
        var trimmed = line.Trim();
        if (!trimmed.StartsWith('{'))
        {
            return null;
        }
        try
        {
            using var document = JsonDocument.Parse(trimmed);
            return document.RootElement.TryGetProperty("run_id", out var value) && value.ValueKind == JsonValueKind.String
                ? value.GetString()
                : null;
        }
        catch (JsonException)
        {
            return null;
        }
    }

    public void Start(string jobName) => _ = StartGuardedAsync(jobName);

    private async Task StartGuardedAsync(string jobName)
    {
        try
        {
            await StartAsync(jobName);
        }
        catch (Exception error)
        {
            // Fire-and-forget boundary: a thrown CliRunner error must not vanish.
            _services.Post(() => _services.Status.Set(error.Message, error: true));
        }
    }

    public async Task StartAsync(string jobName)
    {
        // Checked and set on the UI thread, before the first await: a second "Back up now"
        // must not orphan the first run by replacing its cancellation source.
        if (_running)
        {
            _services.Status.Set($"A backup of '{JobName}' is already running.", error: true);
            return;
        }
        _running = true;
        _finished = false;
        JobName = jobName;
        State = "Starting";
        LogText = "";
        RunId = null;
        CanStop = false;
        Failed = false;
        Succeeded = false;
        Stalled = false;
        Indeterminate = true;
        ProgressValue = 0;
        _mark = -1;
        _marked = Clock();
        _rateBytes = -1;
        _bytesPerSecond = 0;
        _stdout.Clear();
        var cancellation = _cancellation = new CancellationTokenSource();

        try
        {
            var result = await _services.Cli.RunAsync(
                new[] { "job", "run", jobName, "--json" },
                onStdoutLine: line => _services.Post(() => OnStdoutLine(line)),
                cancellationToken: cancellation.Token);

            Finish(result);
        }
        finally
        {
            _finished = true;
            _running = false;
            // The progress watcher polls at 4 Hz forever otherwise: a run shorter than one
            // poll interval deletes its frame before the watcher ever sees it.
            cancellation.Cancel();
        }
    }

    private void OnStdoutLine(string line)
    {
        _stdout.Add(line);
        if (RunId is not null)
        {
            return;
        }
        var runId = TryRunId(line);
        if (runId is null)
        {
            return;
        }
        RunId = runId;
        CanStop = true;
        State = "Scanning · first backup has no percentage";
        _ = WatchAsync(runId, _cancellation!.Token);
    }

    private async Task WatchAsync(string runId, CancellationToken cancellationToken)
    {
        // Both file reads happen here, on the worker; only the resulting strings are marshalled.
        await foreach (var frame in _services.Data.WatchProgress(runId, cancellationToken, () => _finished))
        {
            var tail = _services.Data.LogTail(runId);
            _services.Post(() => Apply(frame, tail));
        }
    }

    public void Apply(ProgressFrame frame, string logTail)
    {
        var done = frame.BytesProcessed ?? 0;
        var uploaded = frame.UploadedBytes ?? 0;
        // A backend can scan ahead, then write the snapshot: once every file is counted, `done`
        // freezes and only `uploaded` climbs. Tracking their sum keeps movement and transfer
        // rate updating through the final write phase.
        var moved = done + uploaded;
        var now = Clock();
        if (moved != _mark)
        {
            _mark = moved;
            _marked = now;
        }
        var quiet = now - _marked >= StallAfter;

        // Transfer rate over the interval since the metric last advanced.
        if (_rateBytes < 0)
        {
            _rateBytes = moved;
            _rateAt = now;
        }
        else if (moved > _rateBytes)
        {
            var seconds = (now - _rateAt).TotalSeconds;
            if (seconds >= 0.5)
            {
                _bytesPerSecond = (moved - _rateBytes) / seconds;
                _rateBytes = moved;
                _rateAt = now;
            }
        }
        var rate = _bytesPerSecond > 0 ? $" · {HumanBytes((long)_bytesPerSecond)}/s" : "";

        if (frame.TotalBytes is > 0)
        {
            Indeterminate = false;
            var total = frame.TotalBytes.Value;
            ProgressMaximum = total;
            ProgressValue = Math.Min(done, total);
            // Cap at 99% until the run actually finishes: an estimated total can be
            // slightly under the real one, and 100% before "Completed" reads as stuck.
            var percent = Math.Min(99, (int)(100.0 * done / total));
            // When hashing is essentially done but bytes are still being flushed, say so —
            // otherwise a steady percentage during a long upload reads as stuck.
            var writing = uploaded > 0 && done >= (long)(total * 0.99);
            var phase = writing ? $" · writing snapshot {HumanBytes(uploaded)}" : "";
            var quietNote = quiet ? " · still connected" : "";
            State = $"{percent}% · {HumanBytes(done)} of {HumanBytes(total)}{phase}{rate}{quietNote}";
            Stalled = quiet;
        }
        else
        {
            Indeterminate = true;
            // No total yet (the source estimate is still being computed). Never a stall.
            Stalled = false;
            State = moved > 0
                ? $"Backing up · {HumanBytes(done)}{rate}"
                : "Scanning for changes… large folders and network shares can take a while";
        }
        LogText = logTail;
    }

    private void Finish(CliResult result)
    {
        CanStop = false;
        Indeterminate = false;
        var success = result.Ok && FinalSuccess() != false;
        State = result.Cancelled ? "Cancelled" : success ? "Completed" : "Failed";
        Failed = !success && !result.Cancelled;
        Succeeded = success && !result.Cancelled;
        Stalled = false;
        if (RunId is not null)
        {
            var tail = _services.Data.LogTail(RunId);
            if (tail.Length > 0)
            {
                LogText = tail;
            }
        }
        if (!success && !result.Cancelled)
        {
            // The CLI's own wording, verbatim.
            var text = result.FailureText;
            LogText = LogText.Length > 0 ? LogText + "\n" + text : text;
            _services.Status.Set(text, error: true);
        }
        else
        {
            _services.Status.Set(State, error: result.Cancelled);
        }
    }

    /// <summary>The final JSON line, when the CLI printed one, wins over the exit code.</summary>
    private bool? FinalSuccess()
    {
        for (var index = _stdout.Count - 1; index >= 0; index--)
        {
            var line = _stdout[index].Trim();
            if (!line.StartsWith('{'))
            {
                continue;
            }
            try
            {
                using var document = JsonDocument.Parse(line);
                if (document.RootElement.TryGetProperty("success", out var value)
                    && value.ValueKind is JsonValueKind.True or JsonValueKind.False)
                {
                    return value.GetBoolean();
                }
            }
            catch (JsonException)
            {
                // not a result line
            }
        }
        return null;
    }

    [RelayCommand]
    public void Stop()
    {
        CanStop = false;
        _cancellation?.Cancel();
        _services.Status.Set(CliRunner.StopWording("backup"), error: true);
    }

    /// <summary>After a failure: the whole log and error text, for pasting into a bug report.</summary>
    [RelayCommand]
    public Task CopyErrorAsync() => _services.CopyText(LogText);

    [RelayCommand]
    public void OpenLogFolder()
    {
        var directory = System.IO.Path.Combine(_services.Data.DataDir, "logs");
        _services.OpenFolder(directory);
        _services.Status.Set($"Logs: {directory}");
    }

    // Leaving the view does not stop the run — only Stop does.
}
