using System;
using System.Threading.Tasks;

namespace Backer.Desktop.Services;

/// <summary>A modal the user must accept before a destructive CLI call is made.</summary>
/// <param name="TypedConfirmation">When set, the user must type this word exactly.</param>
public sealed record ConfirmRequest(string Title, string Body, string ConfirmLabel, string? TypedConfirmation = null);

/// <summary>
/// Everything the view models need from the outside world. Defaults are the safe
/// headless ones so unit tests can build a view model without an Avalonia app:
/// confirmations are declined (fail closed) and callbacks run inline.
/// </summary>
public sealed class AppServices
{
    public ConfigStore Config { get; set; } = new();

    public DataDirStore Data { get; set; } = new();

    public CliRunner Cli { get; set; } = new();

    public StatusService Status { get; set; } = new();

    /// <summary>Marshals a callback onto the UI thread.</summary>
    public Action<Action> Post { get; set; } = action => action();

    public Func<ConfirmRequest, Task<bool>> Confirm { get; set; } = _ => Task.FromResult(false);

    public Func<Task<string?>> PickFolder { get; set; } = () => Task.FromResult<string?>(null);

    /// <summary>Clipboard, wired to the window by App; a no-op in tests.</summary>
    public Func<string, Task> CopyText { get; set; } = _ => Task.CompletedTask;

    public GuiStateStore StateStore { get; set; } = new();

    private NotificationService? _notifications;

    public NotificationService Notifications
    {
        get => _notifications ??= new NotificationService(StateStore, Data);
        set => _notifications = value;
    }

    /// <summary>Opens a folder in the platform file manager.</summary>
    public Action<string> OpenFolder { get; set; } = OpenFolderDefault;

    private static void OpenFolderDefault(string path)
    {
        try
        {
            System.IO.Directory.CreateDirectory(path);
            using var process = System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(path)
            {
                UseShellExecute = true,
            });
        }
        catch (Exception error) when (error is System.ComponentModel.Win32Exception
            or System.IO.IOException or UnauthorizedAccessException or InvalidOperationException)
        {
            // No file manager available; the path is still shown in the status strip.
        }
    }
}
