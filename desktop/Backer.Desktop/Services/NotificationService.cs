using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Linq;

namespace Backer.Desktop.Services;

public sealed record Notification(string Job, string Title, string Body);

/// <summary>
/// Failure notifications at most once per job per UTC day; the first success once per job;
/// a needs-input run once per run id. State lives in the GUI's own app data.
/// </summary>
public sealed class NotificationService
{
    private readonly GuiStateStore _store;
    private readonly DataDirStore _data;

    public NotificationService(GuiStateStore store, DataDirStore data)
    {
        _store = store;
        _data = data;
        State = store.Load();
        Notify = Deliver;
    }

    public GuiState State { get; }

    /// <summary>Shows the notification. Replaced in tests.</summary>
    public Action<Notification> Notify { get; set; }

    /// <summary>
    /// The platform notifier. Returns false when the platform has none (Windows: no toast
    /// without a registered AppUserModelID) or when it failed, which routes the message to
    /// the in-app banner instead.
    /// </summary>
    public Func<Notification, bool> DesktopNotifier { get; set; } = Send;

    /// <summary>Wired by App to the status strip banner; the Windows fallback path.</summary>
    public Action<string>? Banner { get; set; }

    /// <summary>Throttling has already happened by here: this only chooses the channel.</summary>
    public void Deliver(Notification notification)
    {
        if (!DesktopNotifier(notification))
        {
            Banner?.Invoke(notification.Body);
        }
    }

    /// <summary>The close-to-tray hint is shown exactly once, ever. True means "show it now".</summary>
    public bool CloseHintOnce()
    {
        if (State.CloseHintSeen)
        {
            return false;
        }
        State.CloseHintSeen = true;
        _store.Save(State);
        return true;
    }

    public int AttentionCount => State.Attention.Count;

    public string TrayTooltip => AttentionCount == 0
        ? "Backer"
        : AttentionCount == 1
            ? "Backer - 1 backup needs attention"
            : $"Backer - {AttentionCount} backups need attention";

    public static string Today() => DateTime.UtcNow.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);

    /// <summary>Port of the Tk policy in app.py:notification_allowed.</summary>
    public static bool Allowed(GuiState state, string job, JobRun run, string today)
    {
        var status = run.Status ?? "";
        if (status == "cancelled")
        {
            return false;
        }
        if (run.NeedsInput == true)
        {
            return !state.Input.TryGetValue(job, out var seen) || seen != (run.RunId ?? "");
        }
        if (status == "success")
        {
            return !state.FirstSuccess.Contains(job);
        }
        return !state.FailureDay.TryGetValue(job, out var day) || day != today;
    }

    public static Notification Describe(string job, JobRun run) =>
        run.NeedsInput == true
            ? new Notification(job, "Backer needs input", $"{job} needs your attention.")
            : run.Status == "success"
                ? new Notification(job, "Backer", $"{job} completed its first backup.")
                : new Notification(job, "Backer", $"{job} backup did not run. Open Backer for details.");

    /// <summary>Records the decision so a restart cannot replay it.</summary>
    public static void Record(GuiState state, string job, JobRun run, string today)
    {
        var runId = run.RunId ?? "";
        if (run.NeedsInput == true)
        {
            state.Input[job] = runId;
        }
        else if (run.Status == "success")
        {
            if (!state.FirstSuccess.Contains(job))
            {
                state.FirstSuccess.Add(job);
            }
        }
        else if (run.Status != "cancelled")
        {
            state.FailureDay[job] = today;
        }

        if (run.Status == "success" && run.NeedsInput != true)
        {
            state.Attention.Remove(job);
        }
        else if (run.Status != "cancelled")
        {
            state.Attention[job] = runId;
        }
    }

    /// <summary>Reads the newest run of every named job and notifies about what the policy allows.</summary>
    public IReadOnlyList<Notification> Poll(IEnumerable<string> jobNames) => Apply(Read(jobNames));

    /// <summary>
    /// The file IO half of Poll — safe to call on a worker thread. It touches no state.
    /// </summary>
    public IReadOnlyList<(string Job, JobRun Run)> Read(IEnumerable<string> jobNames)
    {
        var runs = new List<(string, JobRun)>();
        foreach (var job in jobNames)
        {
            var run = _data.LastAttempt(job) ?? _data.Runs(job, 1).FirstOrDefault();
            if (run is not null)
            {
                runs.Add((job, run));
            }
        }
        return runs;
    }

    /// <summary>The state half of Poll. State is not thread-safe: call this on the UI thread.</summary>
    public IReadOnlyList<Notification> Apply(IReadOnlyList<(string Job, JobRun Run)> runs)
    {
        var today = Today();
        var sent = new List<Notification>();
        var changed = false;
        foreach (var (job, run) in runs)
        {
            if (Allowed(State, job, run, today))
            {
                var notification = Describe(job, run);
                Notify(notification);
                sent.Add(notification);
            }
            var before = State.Attention.TryGetValue(job, out var previous) ? previous : null;
            Record(State, job, run, today);
            changed |= sent.Count > 0 || before != (State.Attention.TryGetValue(job, out var now) ? now : null);
        }
        if (changed)
        {
            _store.Save(State);
        }
        return sent;
    }

    /// <summary>The user opened the failing run: stop counting it as needing attention.</summary>
    public void ClearAttention(string job)
    {
        if (State.Attention.Remove(job))
        {
            _store.Save(State);
        }
    }

    /// <summary>
    /// notify-send on Linux. Windows has no maintained cross-platform toast library that
    /// works against this Avalonia version (DesktopNotifications.Avalonia is pinned to
    /// Avalonia 0.10), so there it returns false and the caller falls back to the tray
    /// tooltip plus the in-app banner. Recorded in desktop/README.md.
    /// </summary>
    private static bool Send(Notification notification)
    {
        if (!OperatingSystem.IsLinux())
        {
            return false;
        }
        try
        {
            using var process = Process.Start(new ProcessStartInfo("notify-send")
            {
                ArgumentList = { notification.Title, notification.Body },
                UseShellExecute = false,
                CreateNoWindow = true,
            });
            return process is not null;
        }
        catch (Exception error) when (error is System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            return false; // notify-send is not installed
        }
    }
}
