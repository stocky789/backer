using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Input;
using Avalonia.Input.Platform;
using Avalonia.Markup.Xaml;
using Avalonia.Platform.Storage;
using Avalonia.Styling;
using Avalonia.Threading;
using Backer.Desktop.Services;
using Backer.Desktop.ViewModels;
using Backer.Desktop.Views;

namespace Backer.Desktop;

public partial class App : Application
{
    public override void Initialize() => AvaloniaXamlLoader.Load(this);

    public override void OnFrameworkInitializationCompleted()
    {
        if (ApplicationLifetime is IClassicDesktopStyleApplicationLifetime desktop)
        {
            var window = new MainWindow();
            var services = new AppServices
            {
                Post = action => Dispatcher.UIThread.Post(action),
                // Avalonia refuses a dialog owned by a hidden window ("Cannot show window with
                // non-visible owner"), and a confirmation can come from the tray while the window
                // is closed to tray — so the owner is restored first, every time.
                Confirm = request =>
                {
                    if (!window.IsVisible)
                    {
                        window.Show();
                        window.WindowState = WindowState.Normal;
                        window.Activate();
                    }
                    return ConfirmDialog.ShowAsync(window, request);
                },
                PickFolder = async () =>
                {
                    var folders = await window.StorageProvider.OpenFolderPickerAsync(
                        new FolderPickerOpenOptions { AllowMultiple = false });
                    return folders.Count > 0 ? folders[0].TryGetLocalPath() : null;
                },
                CopyText = async text =>
                {
                    if (window.Clipboard is { } clipboard)
                    {
                        await clipboard.SetValueAsync(DataFormat.Text, text);
                    }
                },
            };
            var shell = new MainWindowViewModel(services);
            window.DataContext = shell;

            var state = services.StateStore.Load();
            ApplyTheme(state.Theme);
            shell.Settings.ThemeChanged = ApplyTheme;
            // Windows has no desktop notification here: the message lands in the in-app banner.
            services.Notifications.Banner = text => Dispatcher.UIThread.Post(() => services.Status.Attention = text);

            var tray = new TrayController(shell, window);
            tray.Start();
            window.Closing += (_, e) =>
            {
                // ExitAsync calls desktop.Shutdown(), which closes this window again. Let that
                // close through instead of recursively starting another ExitAsync.
                if (tray.IsExiting)
                {
                    return;
                }
                e.Cancel = true;
                if (tray.Available)
                {
                    window.Hide();
                    ShowCloseHint(services);
                    return;
                }
                _ = tray.ExitAsync();
            };
            if (tray.Available)
            {
                // Closing the window leaves the tray icon (and the scheduler) running.
                desktop.ShutdownMode = ShutdownMode.OnExplicitShutdown;
            }

            // config.yaml is also written by the CLI and the scheduler; follow it (debounced).
            services.Config.Changed += (_, _) => Dispatcher.UIThread.Post(shell.OnConfigChanged);
            services.Config.StartWatching();
            desktop.ShutdownRequested += (_, e) =>
            {
                // Quitting Backer must not leave a backup running (it would orphan and hold the
                // run lock). Stop every live CLI child first, then dispose the config watcher.
                try
                {
                    Services.CliRunner.StopAllAsync().GetAwaiter().GetResult();
                }
                catch (System.Exception)
                {
                    // best-effort: never block the exit on cleanup
                }
                services.Config.Dispose();
            };

            shell.Start();
            desktop.MainWindow = window;
        }
        base.OnFrameworkInitializationCompleted();
    }

    /// <summary>Shown once: closing the window is not quitting.</summary>
    private static void ShowCloseHint(AppServices services)
    {
        if (!services.Notifications.CloseHintOnce())
        {
            return;
        }
        services.Notifications.Notify(new Notification(
            "", "Backer", "Backer is still in the tray. Scheduled backups keep running; use Exit to quit."));
    }

    /// <summary>Settings calls this with "light", "dark" or anything else for the system default.</summary>
    public static void ApplyTheme(string? theme) =>
        Current!.RequestedThemeVariant = theme?.ToLowerInvariant() switch
        {
            "light" => ThemeVariant.Light,
            "dark" => ThemeVariant.Dark,
            _ => ThemeVariant.Default,
        };
}
