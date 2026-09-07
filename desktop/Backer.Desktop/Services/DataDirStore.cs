using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading;
using System.Threading.Tasks;

namespace Backer.Desktop.Services;

public sealed class JobRunResult
{
    public bool? Success { get; set; }
    public long? BytesTransferred { get; set; }
    public long? FilesTransferred { get; set; }
    public double? DurationSeconds { get; set; }
    public List<string>? Errors { get; set; }
}

/// <summary>
/// JobRun.to_dict() (src/backer/core/job.py). All fields optional: older records predate some of
/// them. Pinned against the real writer by Fixtures/data/ and MetadataFixtureTests.
/// </summary>
public sealed class JobRun
{
    public string? JobName { get; set; }
    public string? RunId { get; set; }
    public string? Status { get; set; }
    public string? StartedAt { get; set; }
    public string? FinishedAt { get; set; }
    public string? ErrorMessage { get; set; }
    public string? ClientId { get; set; }
    public string? RepositoryId { get; set; }
    public string? ErrorStage { get; set; }
    public bool? NeedsInput { get; set; }
    public JobRunResult? Result { get; set; }
}

/// <summary>
/// Live progress frame, written by serverless/runs.py::_write_progress. Engine-dependent, and the
/// key set differs between the "started" and "running" frames, so every field stays optional.
/// total_bytes is an estimate from the previous snapshot: null means "unknown", never "nothing left".
/// Pinned against the real writer by Fixtures/data/progress/ and MetadataFixtureTests.
/// </summary>
public sealed class ProgressFrame
{
    public string? RunId { get; set; }
    public string? Status { get; set; }
    public string? StartedAt { get; set; }
    public long? BytesProcessed { get; set; }
    public long? TotalBytes { get; set; }
    public long? FilesProcessed { get; set; }
    public long? TotalFiles { get; set; }
    public long? HashedBytes { get; set; }
    public long? CachedBytes { get; set; }

    /// <summary>
    /// Bytes flushed to the repository so far. Over a slow link the hashed counters plateau
    /// while this keeps climbing, so it is what shows the backup is still working — and the
    /// only honest source for the transfer speed to the destination.
    /// </summary>
    public long? UploadedBytes { get; set; }
}

public sealed record PauseState(bool Paused, string? Until);

/// <summary>Read-only view of &lt;data_dir&gt;. Nothing here mutates state.</summary>
public sealed class DataDirStore
{
    private static readonly JsonSerializerOptions Options = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        NumberHandling = JsonNumberHandling.AllowReadingFromString,
    };

    public DataDirStore(string? dataDir = null) => DataDir = dataDir ?? BackerPaths.DataDir();

    public string DataDir { get; }

    private static T? ReadJson<T>(string path) where T : class
    {
        try
        {
            // FileShare.Delete is required: the writer replaces this file atomically (os.replace),
            // which Windows refuses while a reader holds a handle without it — and the writer is
            // the running backup. Reading must never be able to stop a backup.
            using var stream = new FileStream(
                path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            return JsonSerializer.Deserialize<T>(stream, Options);
        }
        catch (Exception error) when (error is JsonException or IOException or UnauthorizedAccessException
            or NotSupportedException)
        {
            // Missing, half-written or mid-replace: "no data this tick", never an error.
            return null;
        }
    }

    public JobRun? LastAttempt(string jobName) =>
        ReadJson<JobRun>(Path.Combine(DataDir, "last_attempt", BackerPaths.JobSubfolder(jobName) + ".json"));

    /// <summary>Run history for a job, newest first.</summary>
    public IReadOnlyList<JobRun> Runs(string jobName, int limit = 20)
    {
        var directory = Path.Combine(DataDir, "runs", BackerPaths.JobSubfolder(jobName));
        if (!Directory.Exists(directory))
        {
            return Array.Empty<JobRun>();
        }
        return Directory.EnumerateFiles(directory, "*.json")
            .OrderByDescending(Path.GetFileName, StringComparer.Ordinal)
            .Take(limit)
            .Select(ReadJson<JobRun>)
            .Where(run => run is not null)
            .Select(run => run!)
            .ToList();
    }

    /// <summary>Absence means "not running" (the frame is deleted on completion).</summary>
    public ProgressFrame? Progress(string runId) =>
        ReadJson<ProgressFrame>(Path.Combine(DataDir, "progress", runId + ".json"));

    /// <summary>
    /// Poll progress/&lt;run_id&gt;.json at ~4 Hz. Ends once the frame has appeared and
    /// then disappeared (the runner deletes it on completion), once <paramref name="hasFinished"/>
    /// says the CLI has exited with no frame on disk, or on cancellation.
    /// </summary>
    /// <param name="hasFinished">
    /// True when the CLI process has exited. A run shorter than one poll interval deletes its
    /// frame before we ever see it, so "seen" alone would never end the loop.
    /// </param>
    public async IAsyncEnumerable<ProgressFrame> WatchProgress(
        string runId,
        [EnumeratorCancellation] CancellationToken cancellationToken = default,
        Func<bool>? hasFinished = null)
    {
        var seen = false;
        while (!cancellationToken.IsCancellationRequested)
        {
            var frame = Progress(runId);
            if (frame is null)
            {
                if (seen || hasFinished?.Invoke() == true)
                {
                    yield break;
                }
            }
            else
            {
                seen = true;
                yield return frame;
            }
            try
            {
                await Task.Delay(250, cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                yield break;
            }
        }
    }

    /// <summary>
    /// Tail of the per-run log. The runner keeps a head plus a tail of the output (serverless/runs.py),
    /// so the failure lines this returns are the real end of the run, not a truncation boundary.
    /// </summary>
    public string LogTail(string runId, int lines = 200)
    {
        var path = Path.Combine(DataDir, "logs", runId + ".log");
        try
        {
            if (!File.Exists(path))
            {
                return "";
            }
            using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            using var reader = new StreamReader(stream);
            var all = reader.ReadToEnd().Split('\n');
            return string.Join("\n", all.Skip(Math.Max(0, all.Length - lines))).TrimEnd();
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            return "";
        }
    }

    /// <summary>Scheduler pause state from schedule-runtime.json.</summary>
    public PauseState Pause()
    {
        var path = Path.Combine(DataDir, "schedule-runtime.json");
        var runtime = ReadJson<Dictionary<string, JsonElement>>(path);
        if (runtime is null || !runtime.TryGetValue("pause", out var pause) || pause.ValueKind != JsonValueKind.Object)
        {
            return new PauseState(false, null);
        }
        var paused = pause.TryGetProperty("paused", out var flag) && flag.ValueKind == JsonValueKind.True;
        string? until = null;
        if (pause.TryGetProperty("until", out var value) && value.ValueKind == JsonValueKind.String)
        {
            until = value.GetString();
        }
        return new PauseState(paused, until);
    }

    /// <summary>True when run.lock is held, i.e. a local run is in progress.</summary>
    public bool RunInProgress()
    {
        var path = Path.Combine(DataDir, "run.lock");
        if (!File.Exists(path))
        {
            return false;
        }
        try
        {
            using var stream = new FileStream(path, FileMode.Open, FileAccess.ReadWrite, FileShare.None);
            return false;
        }
        catch (IOException)
        {
            return true;
        }
        catch (UnauthorizedAccessException)
        {
            // Owned by SYSTEM/root (the unattended scheduler): assume it is live.
            return true;
        }
    }
}
