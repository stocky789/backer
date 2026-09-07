using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Platform;
using Avalonia.Threading;
using Backer.Desktop.ViewModels;

namespace Backer.Desktop.Views;

/// <summary>
/// The tray icon, on Windows and Linux alike. It only ever navigates or spawns the CLI
/// through the shell view models — no engine work happens here.
/// </summary>
public sealed class TrayController : IDisposable
{
    private readonly MainWindowViewModel _shell;
    private readonly Window _window;
    private readonly DispatcherTimer _timer;

    private TrayIcon? _icon;

    public TrayController(MainWindowViewModel shell, Window window)
    {
        _shell = shell;
        _window = window;
        _timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(30) };
        _timer.Tick += (_, _) => Poll();
    }

    /// <summary>False when the platform refused a tray icon; the window then closes normally.</summary>
    public bool Available => _icon is not null;

    /// <summary>True once quit is confirmed so the resulting window close is not intercepted.</summary>
    public bool IsExiting { get; private set; }

    public void Start()
    {
        try
        {
            _icon = new TrayIcon
            {
                Icon = new WindowIcon(AssetLoader.Open(new Uri("avares://backer-desktop/Assets/backer.ico"))),
                ToolTipText = "Backer",
                IsVisible = true,
            };
            _icon.Clicked += (_, _) => ShowWindow();
        }
        catch (Exception error) when (error is IOException or InvalidOperationException or NotSupportedException)
        {
            _icon = null; // headless or no system tray: fall back to a plain window
            return;
        }
        TrayIcon.SetIcons(Application.Current!, new TrayIcons { _icon });
        Rebuild();
        _timer.Start();
        Poll();
    }

    public void ShowWindow()
    {
        _window.Show();
        _window.WindowState = WindowState.Normal;
        _window.Activate();
    }

    /// <summary>config.yaml and one last_attempt file per job are read on a worker; only the
    /// resulting state change is applied on the UI thread.</summary>
    private void Poll()
    {
        var notifications = _shell.Services.Notifications;
        System.Threading.Tasks.Task.Run(() =>
        {
            var names = _shell.JobNames();
            var runs = notifications.Read(names);
            Dispatcher.UIThread.Post(() =>
            {
                notifications.Apply(runs);
                if (_icon is not null)
                {
                    _icon.ToolTipText = notifications.TrayTooltip;
                }
                Rebuild(names);
            });
        });
    }

    private void Rebuild(IReadOnlyList<string>? jobNames = null)
    {
        if (_icon is null)
        {
            return;
        }
        var menu = new NativeMenu();
        menu.Add(Item("Open Backer", ShowWindow));

        var backups = new NativeMenuItem("Back up now");
        var jobs = new NativeMenu();
        foreach (var job in jobNames ?? _shell.JobNames())
        {
            var name = job;
            jobs.Add(Item(name, () =>
            {
                ShowWindow();
                _shell.ShowRun(name);
            }));
        }
        backups.Menu = jobs;
        backups.IsEnabled = jobs.Items.Count > 0;
        menu.Add(backups);

        if (_shell.Settings.Paused)
        {
            menu.Add(Item("Resume backups", () => _ = _shell.Settings.ResumeAsync()));
        }
        else
        {
            var pause = new NativeMenuItem("Pause backups");
            var durations = new NativeMenu();
            durations.Add(Item("For 1 hour", () => _ = _shell.Settings.PauseOneHourAsync()));
            durations.Add(Item("Until tomorrow", () => _ = _shell.Settings.PauseUntilTomorrowAsync()));
            durations.Add(Item("Until turned back on", () => _ = _shell.Settings.PauseAsync()));
            pause.Menu = durations;
            menu.Add(pause);
        }

        foreach (var (job, _) in _shell.Services.Notifications.State.Attention.ToList())
        {
            var name = job;
            menu.Add(Item($"Open failed run: {name}", () =>
            {
                ShowWindow();
                _shell.ShowFailure(name);
            }));
        }

        menu.Add(Item("Open logs folder", () =>
            _shell.Services.OpenFolder(Path.Combine(_shell.Services.Data.DataDir, "logs"))));
        menu.Add(Item("Settings", () =>
        {
            ShowWindow();
            _shell.Navigate("settings");
        }));
        menu.Add(new NativeMenuItemSeparator());
        menu.Add(Item("Exit", () => _ = ExitAsync()));
        _icon.Menu = menu;
    }

    /// <summary>
    /// Quitting mid-run is one of the five confirmed actions. The tray discards the task, so a
    /// fault here would be silent: it is reported in the status strip instead.
    /// </summary>
    public async System.Threading.Tasks.Task ExitAsync()
    {
        try
        {
            if (!await _shell.ConfirmQuitAsync())
            {
                return;
            }
        }
        catch (Exception error)
        {
            _shell.Status.Set(error.Message, error: true);
            return;
        }
        IsExiting = true;
        Dispose();
        if (Application.Current?.ApplicationLifetime is IClassicDesktopStyleApplicationLifetime desktop)
        {
            desktop.Shutdown();
        }
    }

    private static NativeMenuItem Item(string header, Action action)
    {
        var item = new NativeMenuItem(header);
        item.Click += (_, _) => action();
        return item;
    }

    public void Dispose()
    {
        _timer.Stop();
        _icon?.Dispose();
        _icon = null;
    }
}
