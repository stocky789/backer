using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Globalization;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using Backer.Desktop.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Backer.Desktop.ViewModels;

public sealed partial class JobRow : ObservableObject
{
    public required string Job { get; init; }

    public required string Source { get; init; }

    public required string Repository { get; init; }

    public required string Schedule { get; init; }

    /// <summary>Config id of the repository, needed by `job rm --repo`.</summary>
    public required string RepositoryId { get; init; }

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(IsOk), nameof(NeedsAttention), nameof(NotRunYet))]
    private string _last = "…";

    [ObservableProperty]
    private string _size = "…";

    /// <summary>Row status, derived from the title-cased status Summarize produced.</summary>
    public bool IsOk => Last is "Success" or "Completed";

    public bool NeedsAttention =>
        Last.Contains("Fail", StringComparison.OrdinalIgnoreCase)
        || Last.Contains("Error", StringComparison.OrdinalIgnoreCase);

    public bool NotRunYet => !IsOk && !NeedsAttention;
}

public sealed class HomeRepositoryRow
{
    public required string Name { get; init; }

    public required string Type { get; init; }

    /// <summary>Where the backups live: a path, a share, or an S3 URL — derived from the config.</summary>
    public required string Location { get; init; }

    /// <summary>local = a path; smb = \\server\share; s3 = s3://bucket/prefix.</summary>
    public static string LocationOf(RepositoryConfig repository) => repository.Type switch
    {
        "smb" => $@"\\{repository.Server}\{repository.Share}",
        "s3" => repository.Prefix is { Length: > 0 } prefix
            ? $"s3://{repository.Bucket}/{prefix}"
            : $"s3://{repository.Bucket}",
        _ => repository.Path ?? "",
    };
}

public sealed partial class HomeViewModel : ViewModelBase
{
    private const int RefreshIntervalMs = 60_000;

    private readonly AppServices _services;
    private readonly MainWindowViewModel? _shell;
    private readonly Timer _refreshTimer;

    public HomeViewModel(AppServices services, MainWindowViewModel? shell = null)
    {
        _services = services;
        _shell = shell;
        // The timer fires on a pool thread; Jobs is UI-bound, so the snapshot is taken on the
        // UI thread. An exception escaping a Timer callback would kill the process outright.
        _refreshTimer = new Timer(
            _ =>
            {
                try
                {
                    _services.Post(StartRefresh);
                }
                catch (Exception error)
                {
                    _services.Status.Set(error.Message, error: true);
                }
            },
            null,
            Timeout.Infinite,
            Timeout.Infinite);
    }

    public override string Title => "Local backups";

    public ObservableCollection<JobRow> Jobs { get; } = new();

    public ObservableCollection<HomeRepositoryRow> Repositories { get; } = new();

    /// <summary>A job needs somewhere to keep its snapshots, so New backup job is gated on this.</summary>
    public bool HasRepositories => Repositories.Count > 0;

    public bool NoRepositories => Repositories.Count == 0;

    [ObservableProperty]
    private bool _serverManaged;

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(HasSelection))]
    private JobRow? _selectedJob;

    public bool HasSelection => SelectedJob is not null;

    public bool IsEmpty => Jobs.Count == 0;

    /// <summary>Newest last-run timestamp across the jobs; null until a refresh has landed.</summary>
    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(ProtectionText))]
    private DateTimeOffset? _lastBackupAt;

    /// <summary>Replaced in tests so the relative wording can be checked without waiting.</summary>
    public Func<DateTimeOffset> Clock { get; set; } = () => DateTimeOffset.Now;

    // The protection banner. Every part of it is derived from the rows above — no new state.
    public bool AllProtected => Jobs.Count > 0 && Jobs.All(row => row.IsOk);

    public bool NeedsAttention => Jobs.Any(row => row.NeedsAttention);

    public string ProtectionText
    {
        get
        {
            var count = Jobs.Count;
            if (count == 0)
            {
                return "No local backups yet";
            }
            if (NeedsAttention)
            {
                var failing = Jobs.Count(row => row.NeedsAttention);
                return failing == 1 ? "1 backup needs attention" : $"{failing} backups need attention";
            }
            if (!AllProtected)
            {
                return count == 1
                    ? "1 backup job set up · nothing has run yet"
                    : $"{count} backup jobs set up · nothing has run yet";
            }
            var subject = count == 1 ? "1 backup job protected" : $"All {count} backup jobs protected";
            var when = Relative(LastBackupAt, Clock());
            return when.Length == 0 ? subject : subject + " · last backup " + when;
        }
    }

    /// <summary>Plain relative wording for the banner. Anything older than a week gets a date.</summary>
    public static string Relative(DateTimeOffset? when, DateTimeOffset now)
    {
        if (when is not { } moment)
        {
            return "";
        }
        var elapsed = now - moment;
        if (elapsed < TimeSpan.Zero || elapsed < TimeSpan.FromMinutes(2))
        {
            return "just now";
        }
        if (elapsed < TimeSpan.FromHours(1))
        {
            return $"{(int)elapsed.TotalMinutes} minutes ago";
        }
        if (elapsed < TimeSpan.FromDays(1))
        {
            var hours = (int)elapsed.TotalHours;
            return hours == 1 ? "1 hour ago" : $"{hours} hours ago";
        }
        if (elapsed < TimeSpan.FromDays(7))
        {
            var days = (int)elapsed.TotalDays;
            return days == 1 ? "yesterday" : $"{days} days ago";
        }
        return "on " + moment.ToString("d MMM yyyy", CultureInfo.InvariantCulture);
    }

    private void BannerChanged()
    {
        OnPropertyChanged(nameof(AllProtected));
        OnPropertyChanged(nameof(NeedsAttention));
        OnPropertyChanged(nameof(ProtectionText));
    }

    public bool CanAddRepository => Cells.SupportedRepositoryTypes().Count > 0;

    public override IRelayCommand? PrimaryCommand => CanAddRepository ? AddRepositoryCommand : null;

    public override void OnShown()
    {
        Reload();
        _refreshTimer.Change(0, RefreshIntervalMs);
    }

    public override void OnHidden() => _refreshTimer.Change(Timeout.Infinite, Timeout.Infinite);

    public void Reload()
    {
        BackerConfig config;
        try
        {
            config = _services.Config.Load();
        }
        catch (Exception error)
        {
            _services.Status.Set(error.Message, error: true);
            return;
        }
        var selected = SelectedJob?.Job;
        Jobs.Clear();
        foreach (var (name, job) in config.Jobs.OrderBy(item => item.Key, StringComparer.OrdinalIgnoreCase))
        {
            var repositoryId = job.Repository ?? "";
            config.Repositories.TryGetValue(repositoryId, out var repository);
            Jobs.Add(new JobRow
            {
                Job = name,
                Source = job.Source?.Path ?? "",
                Repository = repository?.Name ?? "Missing",
                Schedule = job.Schedule?.Cron ?? "Manual",
                RepositoryId = repositoryId,
            });
        }
        SelectedJob = Jobs.FirstOrDefault(row => row.Job == selected);

        Repositories.Clear();
        foreach (var (id, repository) in config.Repositories
                     .OrderBy(item => item.Value.Name ?? item.Key, StringComparer.OrdinalIgnoreCase))
        {
            Repositories.Add(new HomeRepositoryRow
            {
                Name = repository.Name ?? id,
                Type = repository.Type ?? "",
                Location = HomeRepositoryRow.LocationOf(repository),
            });
        }

        ServerManaged = config.Server is not null;
        OnPropertyChanged(nameof(IsEmpty));
        OnPropertyChanged(nameof(HasRepositories));
        OnPropertyChanged(nameof(NoRepositories));
        BannerChanged();
    }

    /// <summary>
    /// Called on the UI thread: snapshots the row names there, then reads the data dir on a
    /// worker and marshals the result back.
    /// </summary>
    public void StartRefresh()
    {
        var generation = Generation;
        var names = Jobs.Select(row => row.Job).ToArray();
        if (names.Length == 0)
        {
            return;
        }
        Task.Run(() =>
        {
            var runs = names.Select(name => (Job: name, Run: Newest(name))).ToList();
            var summaries = runs.Select(item => (item.Job, Summary: Summarize(item.Run))).ToList();
            var newest = runs
                .Select(item => Started(item.Run))
                .Where(started => started is not null)
                .DefaultIfEmpty(null)
                .Max();
            _services.Post(() =>
            {
                ApplySummaries(generation, summaries);
                if (generation == Generation && IsVisible)
                {
                    LastBackupAt = newest;
                }
            });
        });
    }

    private JobRun? Newest(string jobName)
    {
        try
        {
            // ponytail: local records only — the repository-side sidecar needs the keystore,
            // which the GUI is not allowed to touch. Add via a CLI JSON command if it matters.
            var candidates = new List<JobRun?> { _services.Data.LastAttempt(jobName) };
            candidates.AddRange(_services.Data.Runs(jobName, 1));
            return candidates.Where(run => run is not null).MaxBy(run => run!.StartedAt ?? "");
        }
        catch (Exception error) when (error is System.IO.IOException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    public void ApplySummaries(int generation, IReadOnlyList<(string Job, (string Last, string Size) Summary)> summaries)
    {
        if (generation != Generation || !IsVisible)
        {
            return; // the user navigated away (or back) since this refresh started
        }
        foreach (var (job, summary) in summaries)
        {
            var row = Jobs.FirstOrDefault(item => item.Job == job);
            if (row is null)
            {
                continue;
            }
            row.Last = summary.Last;
            row.Size = summary.Size;
        }
        BannerChanged();
    }

    /// <summary>The run's start time, when it recorded one this code can read.</summary>
    private static DateTimeOffset? Started(JobRun? run) =>
        run?.StartedAt is { Length: > 0 } text
        && DateTimeOffset.TryParse(
            text, CultureInfo.InvariantCulture, DateTimeStyles.AssumeLocal, out var parsed)
            ? parsed
            : null;

    /// <summary>Port of backer.serverless.history.run_summary for a single record.</summary>
    public static (string Last, string Size) Summarize(JobRun? run)
    {
        if (run is null)
        {
            return ("Never run", "—");
        }
        var status = (run.Status ?? "never").Replace("_", " ");
        return (CultureInfo.InvariantCulture.TextInfo.ToTitleCase(status), HumanSize(run.Result?.BytesTransferred ?? 0));
    }

    /// <summary>Port of backer.serverless.history._size.</summary>
    public static string HumanSize(long value)
    {
        if (value == 0)
        {
            return "—";
        }
        string[] units = { "B", "KiB", "MiB", "GiB", "TiB" };
        var amount = (double)value;
        for (var index = 0; index < units.Length - 1; index++)
        {
            if (amount < 1024)
            {
                return string.Format(CultureInfo.InvariantCulture, "{0:F1} {1}", amount, units[index]);
            }
            amount /= 1024;
        }
        return string.Format(CultureInfo.InvariantCulture, "{0:F1} {1}", amount, units[^1]);
    }

    [RelayCommand]
    private void AddRepository() => _shell?.Navigate("repository");

    [RelayCommand]
    private void NewJob() => _shell?.ShowNewJob();

    [RelayCommand]
    private void BackUpNow()
    {
        if (SelectedJob is { } row)
        {
            _shell?.ShowRun(row.Job);
        }
    }

    [RelayCommand]
    private void Restore()
    {
        if (SelectedJob is { } row)
        {
            _shell?.ShowRestore(row.Job);
        }
    }

    [RelayCommand]
    private void Edit()
    {
        if (SelectedJob is { } row)
        {
            _shell?.ShowEditJob(row.Job);
        }
    }

    [RelayCommand]
    public async Task RemoveAsync()
    {
        if (SelectedJob is not { } row)
        {
            return;
        }
        if (row.RepositoryId.Length == 0)
        {
            // `job rm --repo ""` is rejected by the CLI with a misleading message; say the real thing.
            _services.Status.Set(
                $"'{row.Job}' has no repository in config.yaml, so it cannot be removed here. "
                + "Repair the config or remove the job with the CLI.",
                error: true);
            return;
        }
        var confirmed = await _services.Confirm(new ConfirmRequest(
            "Remove backup job",
            $"Remove the job '{row.Job}'? Its snapshots stay in the repository; only the job is removed from this computer.",
            "Remove job"));
        if (!confirmed)
        {
            _services.Status.Set("Nothing was removed");
            return;
        }
        var result = await _services.Cli.RunAsync(new[] { "job", "rm", row.Job, "--repo", row.RepositoryId, "--yes" });
        if (result.Ok)
        {
            _services.Status.Set(result.Stdout.Trim());
        }
        else
        {
            _services.Status.Set(result.FailureText, error: true);
        }
        Reload();
    }
}
