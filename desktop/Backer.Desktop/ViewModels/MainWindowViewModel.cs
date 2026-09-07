using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using Backer.Desktop.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Backer.Desktop.ViewModels;

/// <summary>
/// Single-window shell: one ContentControl, retained view instances, one visible at a time.
/// </summary>
public sealed partial class MainWindowViewModel : ObservableObject
{
    public const string HomeKey = "home";

    private readonly Dictionary<string, ViewModelBase> _views;

    [ObservableProperty]
    private ViewModelBase _currentView;

    public MainWindowViewModel()
        : this(new AppServices())
    {
    }

    public MainWindowViewModel(AppServices services)
    {
        Services = services;
        Run = new RunViewModel(services);
        Restore = new RestoreViewModel(services, this);
        Home = new HomeViewModel(services, this);
        Settings = new SettingsViewModel(services, this);
        EditJob = new EditJobViewModel(services, this);
        Job = new JobViewModel(services, this);
        _views = new Dictionary<string, ViewModelBase>(StringComparer.Ordinal)
        {
            ["welcome"] = new WelcomeViewModel(this),
            [HomeKey] = Home,
            ["repository"] = new RepositoryViewModel(services, this),
            ["editjob"] = EditJob,
            ["job"] = Job,
            ["run"] = Run,
            ["restore"] = Restore,
            ["settings"] = Settings,
        };
        _currentView = _views[HomeKey];
    }

    public AppServices Services { get; }

    public StatusService Status => Services.Status;

    public RunViewModel Run { get; }

    public RestoreViewModel Restore { get; }

    public HomeViewModel Home { get; }

    public SettingsViewModel Settings { get; }

    public EditJobViewModel EditJob { get; }

    public JobViewModel Job { get; }

    /// <summary>The user has read the in-app attention banner.</summary>
    [RelayCommand]
    public void DismissAttention() => Status.Attention = "";

    /// <summary>Job names for the tray's "Back up now" submenu.</summary>
    public IReadOnlyList<string> JobNames()
    {
        try
        {
            return Services.Config.Load().Jobs.Keys
                .OrderBy(name => name, StringComparer.OrdinalIgnoreCase).ToList();
        }
        catch (Exception error) when (error is System.IO.IOException or UnauthorizedAccessException)
        {
            return Array.Empty<string>();
        }
    }

    /// <summary>
    /// Confirmation site 5 of 5: closing the app down. Both prompts that reach it — quitting
    /// mid-run and running the update installer — stop a backup that may be in flight, so they
    /// share one modal with their own wording.
    /// </summary>
    public Task<bool> ConfirmInterruptAsync(string title, string body, string confirmLabel) =>
        Services.Confirm(new ConfirmRequest(title, body, confirmLabel));

    /// <summary>Quitting mid-run kills the backup, so it is confirmed. True means "go ahead".</summary>
    public async Task<bool> ConfirmQuitAsync()
    {
        if (!Run.CanStop)
        {
            return true;
        }
        return await ConfirmInterruptAsync(
            "Quit Backer",
            "A backup is running. Quitting stops it; the snapshot for this run will not be finished.",
            "Stop the backup and quit");
    }

    /// <summary>Tray: open the failing run's details without starting anything.</summary>
    public void ShowFailure(string jobName)
    {
        Services.Notifications.ClearAttention(jobName);
        Navigate(HomeKey);
        Home.SelectedJob = Home.Jobs.FirstOrDefault(row => row.Job == jobName);
    }

    /// <summary>
    /// config.yaml changed under us (the CLI or the scheduler wrote it). Refresh Home and
    /// switch between Welcome and Home if the config just gained or lost its first repository.
    /// Must be called on the UI thread.
    /// </summary>
    public void OnConfigChanged()
    {
        BackerConfig config;
        try
        {
            config = Services.Config.Load();
        }
        catch (IOException)
        {
            // The watcher fires while the CLI is mid-replace; that tick has no config to read.
            // Keep the previous state — the next write raises the event again.
            return;
        }
        catch (Exception error)
        {
            Status.Set(error.Message, error: true);
            return;
        }
        var empty = config.Repositories.Count == 0 && config.Server is null;
        var onWelcome = ReferenceEquals(CurrentView, _views["welcome"]);
        if (empty)
        {
            Navigate("welcome");
            return;
        }
        if (onWelcome)
        {
            Navigate(HomeKey);
        }
        Home.Reload();
    }

    /// <summary>Startup view: welcome until there is something to show on Home.</summary>
    public void Start()
    {
        BackerConfig config;
        try
        {
            config = Services.Config.Load();
        }
        catch (Exception error)
        {
            Status.Set(error.Message, error: true);
            config = new BackerConfig();
        }
        if (config.Repositories.Count == 0 && config.Server is null)
        {
            Navigate("welcome");
            return;
        }
        Status.Subtitle = CurrentView.Title;
        CurrentView.Enter();
    }

    [RelayCommand]
    public void Navigate(string key)
    {
        if (!_views.TryGetValue(key, out var view) || ReferenceEquals(view, CurrentView))
        {
            return;
        }
        CurrentView.Exit();
        CurrentView = view;
        Status.Subtitle = view.Title;
        view.Enter();
    }

    /// <summary>Home -> Run, carrying the selected job.</summary>
    public void ShowRun(string jobName)
    {
        Navigate("run");
        Run.Start(jobName);
    }

    /// <summary>Home -> Edit job, carrying the selected job.</summary>
    public void ShowEditJob(string jobName)
    {
        EditJob.Load(jobName);
        Navigate("editjob");
    }

    /// <summary>Home -> Restore, carrying the selected job.</summary>
    public void ShowRestore(string jobName)
    {
        Navigate("restore");
        Restore.Start(jobName);
    }

    /// <summary>Home -> New backup job. Navigating loads the repository selector (OnShown).</summary>
    public void ShowNewJob() => Navigate("job");

    /// <summary>Escape.</summary>
    [RelayCommand]
    public void GoHome() => Navigate(HomeKey);

    /// <summary>Enter — runs the visible view's primary command when it is enabled.</summary>
    [RelayCommand]
    public void InvokePrimary()
    {
        var command = CurrentView.PrimaryCommand;
        if (command is not null && command.CanExecute(null))
        {
            command.Execute(null);
        }
    }
}
