using System.Collections.Generic;
using System.Linq;
using CommunityToolkit.Mvvm.ComponentModel;

namespace Backer.Desktop.Services;

/// <summary>Backs the three muted labels in the bottom status strip, plus the detail log.</summary>
public sealed partial class StatusService : ObservableObject
{
    private const int LogLimit = 500;

    private readonly Queue<string> _log = new();

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(IsFailed))]
    private string _status = "OK · Ready";

    /// <summary>Set() prefixes every failure; the strip shows those in Danger.</summary>
    public bool IsFailed => Status.StartsWith("Failed", System.StringComparison.Ordinal);

    [ObservableProperty]
    private string _pauseState = "";

    [ObservableProperty]
    private string _subtitle = "";

    /// <summary>The capped detail log the user can open; failure text is the CLI's own.</summary>
    [ObservableProperty]
    private string _detail = "";

    /// <summary>
    /// In-app attention banner. It carries a notification the platform could not show as a
    /// desktop notification (Windows), so a failed nightly backup is still visible in the app.
    /// </summary>
    [ObservableProperty]
    private string _attention = "";

    public void Set(string message, bool error = false)
    {
        Status = (error ? "Failed · " : "OK · ") + message;
        if (error)
        {
            Append(message);
        }
    }

    public void Append(string text)
    {
        foreach (var line in text.Replace("\r\n", "\n").Split('\n'))
        {
            if (line.Trim().Length == 0)
            {
                continue;
            }
            _log.Enqueue(line);
            while (_log.Count > LogLimit)
            {
                _log.Dequeue();
            }
        }
        Detail = string.Join("\n", _log);
    }

    public IReadOnlyList<string> LogLines => _log.ToList();
}
