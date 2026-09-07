using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace Backer.Desktop.Services;

public sealed record CliResult(int ExitCode, string Stdout, string Stderr, bool Cancelled)
{
    public bool Ok => ExitCode == 0 && !Cancelled;

    /// <summary>The CLI's own wording, shown verbatim (messages-catalogue no-drift rule).</summary>
    public string FailureText
    {
        get
        {
            var text = Stderr.Trim();
            return text.Length > 0 ? text : (Stdout.Trim().Length > 0 ? Stdout.Trim() : StderrOnlyFailureText);
        }
    }

    /// <summary>
    /// Failure wording for a call whose stdout may carry a secret (a generated passphrase is
    /// printed there). Never falls back to stdout.
    /// </summary>
    public string StderrOnlyFailureText
    {
        get
        {
            var text = Stderr.Trim();
            return text.Length > 0 ? text : $"backer exited with code {ExitCode}";
        }
    }

    /// <summary>Null when the CLI printed something that is not the JSON we asked for.</summary>
    public T? Json<T>() => Parse<T>(Stdout);

    /// <summary>First non-empty stdout line parsed as JSON (e.g. `job run --json`'s run_id line).</summary>
    public T? FirstJsonLine<T>()
    {
        foreach (var line in Stdout.Split('\n'))
        {
            var trimmed = line.Trim();
            if (trimmed.StartsWith('{') || trimmed.StartsWith('['))
            {
                return Parse<T>(trimmed);
            }
        }
        return default;
    }

    private static T? Parse<T>(string text)
    {
        try
        {
            return JsonSerializer.Deserialize<T>(text, CliRunner.JsonOptions);
        }
        catch (JsonException)
        {
            return default;
        }
    }
}

/// <summary>
/// Spawns the `backer` CLI. Every mutation the GUI performs goes through here;
/// secrets are written to stdin and never appear in argv or in any log.
/// </summary>
public sealed class CliRunner
{
    internal static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
    };

    private readonly string? _executableOverride;

    public CliRunner(string? executable = null) => _executableOverride = executable;

    public string Executable => _executableOverride ?? Locate();

    /// <summary>BACKER_CLI env -> backer[.exe] beside our own executable -> PATH.</summary>
    public static string Locate()
    {
        var configured = Environment.GetEnvironmentVariable("BACKER_CLI");
        if (!string.IsNullOrEmpty(configured))
        {
            return configured;
        }
        var name = OperatingSystem.IsWindows() ? "backer.exe" : "backer";
        var directory = Path.GetDirectoryName(Environment.ProcessPath);
        if (!string.IsNullOrEmpty(directory))
        {
            var beside = Path.Combine(directory, name);
            if (File.Exists(beside))
            {
                return beside;
            }
        }
        return name; // resolved via PATH by the OS
    }

    /// <summary>
    /// UTF-8 on both ends, pinned. The frozen Windows backer.exe otherwise encodes its redirected
    /// stdout with the ANSI code page (cp1252), where the CLI's own success lines — which contain
    /// U+2713 — raise UnicodeEncodeError *after* the mutation has been committed, so a completed
    /// action is reported as a failure. PYTHONUTF8 fixes the writer, the encodings fix the reader.
    /// </summary>
    public ProcessStartInfo BuildStartInfo(
        IEnumerable<string> arguments,
        IReadOnlyDictionary<string, string>? environment = null)
    {
        var utf8 = new UTF8Encoding(encoderShouldEmitUTF8Identifier: false);
        var info = new ProcessStartInfo(Executable)
        {
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardInputEncoding = utf8,
            StandardOutputEncoding = utf8,
            StandardErrorEncoding = utf8,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        info.Environment["PYTHONUTF8"] = "1";
        foreach (var argument in arguments)
        {
            info.ArgumentList.Add(argument);
        }
        foreach (var (key, value) in environment ?? new Dictionary<string, string>())
        {
            info.Environment[key] = value;
        }
        return info;
    }

    /// <param name="onStdoutLine">
    /// Called on a background thread for each stdout line as it arrives — the only way to
    /// see `job run --json`'s run_id line before the run finishes. Marshal before touching UI.
    /// </param>
    /// <param name="environment">
    /// Extra child-process environment. The CLI refuses two secrets on stdin at once, so a
    /// storage secret travels here while the passphrase takes stdin. Still never argv.
    /// </param>
    public async Task<CliResult> RunAsync(
        IEnumerable<string> arguments,
        string? stdin = null,
        Action<string>? onStdoutLine = null,
        IReadOnlyDictionary<string, string>? environment = null,
        CancellationToken cancellationToken = default)
    {
        var info = BuildStartInfo(arguments, environment);
        using var process = new Process { StartInfo = info };
        var stdout = new StringBuilder();
        var stderr = new StringBuilder();
        process.OutputDataReceived += (_, e) =>
        {
            if (e.Data is null)
            {
                return;
            }
            stdout.AppendLine(e.Data);
            onStdoutLine?.Invoke(e.Data);
        };
        process.ErrorDataReceived += (_, e) => { if (e.Data is not null) { stderr.AppendLine(e.Data); } };

        try
        {
            process.Start();
        }
        catch (System.ComponentModel.Win32Exception)
        {
            // A missing CLI is a message, not a crash.
            return new CliResult(127, "",
                $"The backer command-line tool was not found (looked for '{Executable}'). " +
                "Install it alongside this app, add it to PATH, or set BACKER_CLI to its full path.",
                Cancelled: false);
        }
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
        // Track every live child so a full quit can stop them all — otherwise a running
        // backup keeps going (and orphans, holding the run lock) after the app exits.
        Live[process] = 0;

        try
        {
            if (stdin is not null)
            {
                await process.StandardInput.WriteAsync(stdin).ConfigureAwait(false);
            }
            process.StandardInput.Close();
        }
        catch (Exception error) when (error is IOException or ObjectDisposedException)
        {
            // The CLI rejected the command and exited before reading stdin (broken pipe).
            // Its stderr is still captured, so the real error is reported below.
        }

        var cancelled = false;
        try
        {
            await process.WaitForExitAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            cancelled = true;
            await StopAsync(process).ConfigureAwait(false);
        }
        finally
        {
            Live.TryRemove(process, out _);
        }

        return new CliResult(process.ExitCode, stdout.ToString(), stderr.ToString(), cancelled);
    }

    /// <summary>Live child processes, so app shutdown can stop every running job.</summary>
    private static readonly System.Collections.Concurrent.ConcurrentDictionary<Process, byte> Live = new();

    /// <summary>
    /// Stop every running CLI child (best-effort, bounded). Called when Backer quits so a
    /// running backup is cancelled cleanly instead of being orphaned with the run lock held.
    /// </summary>
    public static async Task StopAllAsync()
    {
        foreach (var process in Live.Keys)
        {
            try
            {
                if (!process.HasExited)
                {
                    await StopAsync(process).ConfigureAwait(false);
                }
            }
            catch (Exception error) when (error is InvalidOperationException or IOException)
            {
                // already gone
            }
            Live.TryRemove(process, out _);
        }
    }

    /// <summary>How long a cooperative stop is given before the tree is killed.</summary>
    public static readonly TimeSpan CancelGrace = TimeSpan.FromSeconds(5);

    /// <summary>
    /// What Stop actually does, per platform — the status text must not promise more.
    /// POSIX gets SIGINT first (the CLI turns that into kopia's own clean stop); Windows has
    /// no equivalent for a redirected child, so the tree is ended immediately.
    /// </summary>
    public static string StopWording(string noun) => OperatingSystem.IsWindows()
        ? $"Stopping the {noun} now. This run ends immediately and its snapshot is not finished."
        : $"Stopping the {noun}. Backer is asked to stop cleanly and is forced to stop after "
          + $"{(int)CancelGrace.TotalSeconds} seconds.";

    private const int Sigint = 2;

    [System.Runtime.InteropServices.DllImport("libc", SetLastError = true, EntryPoint = "kill")]
    private static extern int SysKill(int pid, int signal);

    /// <summary>
    /// SIGINT the child, then kill the tree if it does not go. The signal goes to the child
    /// itself, not to a process group: the child shares ours, so `kill(-pid)` would either fail
    /// or hit an unrelated group. The CLI stops kopia on SIGINT, so one signal reaches the tree.
    /// </summary>
    private static async Task StopAsync(Process process)
    {
        if (!OperatingSystem.IsWindows())
        {
            try
            {
                SysKill(process.Id, Sigint);
                using var grace = new CancellationTokenSource(CancelGrace);
                await process.WaitForExitAsync(grace.Token).ConfigureAwait(false);
                return; // stopped cooperatively; the CLI wrote its cancelled run record
            }
            catch (OperationCanceledException)
            {
                // ignored the signal: fall through to the kill
            }
            catch (Exception error) when (error is InvalidOperationException or EntryPointNotFoundException
                or DllNotFoundException)
            {
                // no libc or already gone: fall through to the kill
            }
        }
        try
        {
            process.Kill(entireProcessTree: true);
        }
        catch (InvalidOperationException)
        {
            // already gone
        }
        await process.WaitForExitAsync(CancellationToken.None).ConfigureAwait(false);
    }
}
