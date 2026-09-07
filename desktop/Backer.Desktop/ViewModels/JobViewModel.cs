using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Linq;
using System.Threading.Tasks;
using Backer.Desktop.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Backer.Desktop.ViewModels;

/// <summary>A repository the user can pick for a new job: the name is shown, the id is sent.</summary>
public sealed class RepositoryChoice
{
    public required string Id { get; init; }

    public required string Name { get; init; }

    public override string ToString() => Name;
}

/// <summary>
/// "New backup job": creates a job against an existing repository. It is `backer job create`
/// with the same field set as the editor — the CLI validates everything and its wording is what
/// the user reads.
/// </summary>
public sealed partial class JobViewModel : ViewModelBase
{
    private readonly AppServices _services;
    private readonly MainWindowViewModel? _shell;

    public JobViewModel(AppServices services, MainWindowViewModel? shell = null)
    {
        _services = services;
        _shell = shell;
    }

    public JobViewModel()
        : this(new AppServices())
    {
    }

    public override string Title => "New backup job";

    public override IRelayCommand PrimaryCommand => CreateCommand;

    public ObservableCollection<RepositoryChoice> Repositories { get; } = new();

    [ObservableProperty]
    private RepositoryChoice? _selectedRepository;

    [ObservableProperty]
    private string _jobName = "";

    [ObservableProperty]
    private string _source = "";

    [ObservableProperty]
    private string _cron = "0 2 * * *";

    [ObservableProperty]
    private bool _noSchedule;

    [ObservableProperty]
    private string _keepLast = "";

    [ObservableProperty]
    private string _keepDaily = "";

    [ObservableProperty]
    private string _keepWeekly = "";

    [ObservableProperty]
    private string _keepMonthly = "";

    [ObservableProperty]
    private string _keepYearly = "";

    /// <summary>One exclude pattern per line; each becomes its own `--exclude`.</summary>
    [ObservableProperty]
    private string _excludes = "";

    [ObservableProperty]
    private string _statusText = "";

    [ObservableProperty]
    private bool _busy;

    public override void OnShown() => LoadRepositories();

    /// <summary>Fills the selector from config.yaml; the only repository is preselected.</summary>
    public void LoadRepositories()
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
        var previous = SelectedRepository?.Id;
        Repositories.Clear();
        foreach (var (id, repository) in config.Repositories
                     .OrderBy(item => item.Value.Name ?? item.Key, StringComparer.OrdinalIgnoreCase))
        {
            Repositories.Add(new RepositoryChoice { Id = repository.Id ?? id, Name = repository.Name ?? id });
        }
        SelectedRepository = Repositories.FirstOrDefault(choice => choice.Id == previous)
            ?? (Repositories.Count == 1 ? Repositories[0] : null);
    }

    /// <summary>argv for `job create`. This is the wizard's old BuildJobCreateArguments, moved here.</summary>
    public IReadOnlyList<string> BuildArguments()
    {
        var arguments = new List<string> { "job", "create", JobName.Trim(), "--source", Source.Trim() };
        if (SelectedRepository is { } repository)
        {
            arguments.AddRange(new[] { "--repo", repository.Id });
        }
        if (NoSchedule)
        {
            arguments.Add("--no-schedule");
        }
        else if (Cron.Trim().Length > 0)
        {
            arguments.AddRange(new[] { "--schedule", Cron.Trim() });
        }
        foreach (var (flag, value) in new[]
                 {
                     ("--keep-last", KeepLast), ("--keep-daily", KeepDaily), ("--keep-weekly", KeepWeekly),
                     ("--keep-monthly", KeepMonthly), ("--keep-yearly", KeepYearly),
                 })
        {
            if (value.Trim().Length > 0)
            {
                arguments.AddRange(new[] { flag, value.Trim() });
            }
        }
        foreach (var pattern in JobFields.ExcludeLines(Excludes))
        {
            arguments.AddRange(new[] { "--exclude", pattern });
        }
        return arguments;
    }

    [RelayCommand]
    public async Task CreateAsync()
    {
        if (SelectedRepository is null)
        {
            // Fail closed before spawning anything: a job needs a repository to keep snapshots in.
            StatusText = Repositories.Count == 0
                ? "Add a repository first — a backup job needs somewhere to keep its snapshots."
                : "Choose a repository for this backup job.";
            return;
        }
        if (JobName.Trim().Length == 0)
        {
            StatusText = "Name the backup job.";
            return;
        }
        if (Source.Trim().Length == 0)
        {
            StatusText = "Choose a folder to back up.";
            return;
        }
        Busy = true;
        StatusText = "Creating the backup job…";
        // The cron string and the keep-* values are validated by the CLI: its error is the one worth reading.
        var result = await _services.Cli.RunAsync(BuildArguments());
        Busy = false;
        if (!result.Ok)
        {
            StatusText = result.FailureText;
            _services.Status.Set(result.FailureText, error: true);
            return;
        }
        _services.Status.Set(result.Stdout.Trim().Split('\n').LastOrDefault() ?? "Backup job created");
        _shell?.Home.Reload();
        _shell?.GoHome();
    }

    [RelayCommand]
    public async Task ChooseSourceAsync()
    {
        var folder = await _services.PickFolder();
        if (folder is not null)
        {
            Source = folder;
        }
    }

    [RelayCommand]
    private void Cancel() => _shell?.GoHome();
}
