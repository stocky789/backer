using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;

namespace Backer.Desktop.Services;

/// <summary>
/// The GUI's own preferences and notification bookkeeping. Deliberately NOT in the agent's
/// data dir — the GUI only ever reads that (D8).
/// </summary>
public sealed class GuiState
{
    /// <summary>"system", "light" or "dark".</summary>
    public string Theme { get; set; } = "system";

    /// <summary>True once the close-to-tray hint has been shown.</summary>
    public bool CloseHintSeen { get; set; }

    /// <summary>job -> run_id of the needs-input run already notified about.</summary>
    public Dictionary<string, string> Input { get; set; } = new();

    /// <summary>Jobs whose first success has been notified.</summary>
    public List<string> FirstSuccess { get; set; } = new();

    /// <summary>job -> yyyy-MM-dd of the last failure notification (UTC).</summary>
    public Dictionary<string, string> FailureDay { get; set; } = new();

    /// <summary>job -> run_id still needing the user's attention; drives the tray tooltip.</summary>
    public Dictionary<string, string> Attention { get; set; } = new();
}

public sealed class GuiStateStore
{
    private static readonly JsonSerializerOptions Options = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        WriteIndented = true,
    };

    public GuiStateStore(string? directory = null) =>
        Directory = directory ?? Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "backer-desktop");

    public string Directory { get; }

    public string StateFile => Path.Combine(Directory, "gui-state.json");

    public GuiState Load()
    {
        try
        {
            return File.Exists(StateFile)
                ? JsonSerializer.Deserialize<GuiState>(File.ReadAllText(StateFile), Options) ?? new GuiState()
                : new GuiState();
        }
        catch (Exception error) when (error is JsonException or IOException or UnauthorizedAccessException)
        {
            return new GuiState();
        }
    }

    /// <summary>Atomic write: a half-written state file must never re-notify the world.</summary>
    public void Save(GuiState state)
    {
        try
        {
            System.IO.Directory.CreateDirectory(Directory);
            var temporary = StateFile + ".tmp";
            File.WriteAllText(temporary, JsonSerializer.Serialize(state, Options));
            File.Move(temporary, StateFile, overwrite: true);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            // Preferences are not worth failing a backup over.
        }
    }
}
