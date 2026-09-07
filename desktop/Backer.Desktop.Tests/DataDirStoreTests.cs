using System;
using System.IO;
using System.Linq;
using Backer.Desktop.Services;
using Xunit;

namespace Backer.Desktop.Tests;

public sealed class DataDirStoreTests : IDisposable
{
    private readonly string _dir = Directory.CreateTempSubdirectory("backer-data").FullName;
    private readonly DataDirStore _store;

    public DataDirStoreTests() => _store = new DataDirStore(_dir);

    public void Dispose() => Directory.Delete(_dir, recursive: true);

    private void Write(string relative, string content)
    {
        var path = Path.Combine(_dir, relative);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllText(path, content);
    }

    private const string SuccessRun = """
        {"job_name": "Daily Docs", "run_id": "20260902T020000Z-1a2b3c4d", "status": "success",
         "started_at": "2026-09-02T02:00:00Z", "finished_at": "2026-09-02T02:04:11Z",
         "error_message": null, "client_id": "agent-1", "repository_id": "9f8e7d6c5b4a",
         "error_stage": null, "needs_input": false,
         "result": {"success": true, "bytes_transferred": 1234, "files_transferred": 42,
                    "duration_seconds": 251.4, "errors": []}}
        """;

    [Fact]
    public void ReadsLastAttemptThroughTheSanitisedSubfolder()
    {
        Write("last_attempt/Daily Docs.json", SuccessRun);
        var run = _store.LastAttempt("Daily Docs");

        Assert.NotNull(run);
        Assert.Equal("success", run!.Status);
        Assert.Equal("20260902T020000Z-1a2b3c4d", run.RunId);
        Assert.Equal(1234, run.Result!.BytesTransferred);
        Assert.Empty(run.Result.Errors!);
        Assert.Null(run.ErrorStage);
    }

    [Fact]
    public void ReadsSparseRunsAndOrdersNewestFirst()
    {
        Write("runs/docs/20260901T020000Z-aaa.json", """{"run_id": "20260901T020000Z-aaa"}""");
        Write("runs/docs/20260902T020000Z-aaa.json", """{"run_id": "20260902T020000Z-aaa", "status": "failed"}""");

        var runs = _store.Runs("docs");
        Assert.Equal(
            new[] { "20260902T020000Z-aaa", "20260901T020000Z-aaa" },
            runs.Select(run => run.RunId));
        Assert.Null(runs[1].Status);
        Assert.Null(runs[0].Result);
    }

    [Fact]
    public void MissingHistoryIsEmpty()
    {
        Assert.Empty(_store.Runs("nothing"));
        Assert.Null(_store.LastAttempt("nothing"));
    }

    [Fact]
    public void ProgressAbsenceMeansNotRunning()
    {
        Assert.Null(_store.Progress("20260902T020000Z-aaa"));

        Write(
            "progress/20260902T020000Z-aaa.json",
            """{"run_id": "20260902T020000Z-aaa", "bytes_processed": 512, "total_bytes": 2048}""");
        var frame = _store.Progress("20260902T020000Z-aaa");

        Assert.Equal(512, frame!.BytesProcessed);
        Assert.Equal(2048, frame.TotalBytes);
        Assert.Null(frame.FilesProcessed);
    }

    [Fact]
    public void CorruptJsonReadsAsNoData()
    {
        Write("last_attempt/docs.json", "{not json");
        Assert.Null(_store.LastAttempt("docs"));
    }

    [Fact]
    public void ReadsPauseState()
    {
        Assert.False(_store.Pause().Paused);

        Write("schedule-runtime.json", """{"pause": {"paused": true, "until": "2026-09-03T09:00:00Z"}}""");
        var pause = _store.Pause();
        Assert.True(pause.Paused);
        Assert.Equal("2026-09-03T09:00:00Z", pause.Until);

        Write("schedule-runtime.json", """{"pause": {"paused": true, "until": null}}""");
        Assert.Null(_store.Pause().Until);
    }

    [Fact]
    public void TailsTheRunLog()
    {
        Write("logs/run-1.log", string.Join("\n", Enumerable.Range(1, 10).Select(i => $"line {i}")));
        Assert.Equal("line 8\nline 9\nline 10", _store.LogTail("run-1", lines: 3));
        Assert.Equal("", _store.LogTail("missing"));
    }

    /// <summary>
    /// The writer replaces progress/&lt;run&gt;.json atomically while we poll it. Our handle must
    /// permit that (FileShare.Delete) — on Windows it would otherwise fail the running backup's
    /// write, and that exception is swallowed inside kopia's stderr reader.
    /// </summary>
    [Fact]
    public void ProgressIsOpenedSoTheWriterCanStillReplaceTheFile()
    {
        Write("progress/run-1.json", """{"run_id": "run-1", "bytes_processed": 5}""");
        var path = Path.Combine(_dir, "progress", "run-1.json");

        using (var reader = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
        {
            // A reader holding the same share mode the store uses must not block a replace.
            var replacement = Path.Combine(_dir, "progress", "run-1.json.tmp");
            File.WriteAllText(replacement, """{"run_id": "run-1", "bytes_processed": 9}""");
            File.Move(replacement, path, overwrite: true);
        }
        Assert.Equal(9, _store.Progress("run-1")!.BytesProcessed);

        // A truncated frame mid-replace is "no frame this tick", never an error.
        Write("progress/run-2.json", "{\"run_id\": \"run-2\", ");
        Assert.Null(_store.Progress("run-2"));
        Assert.Null(_store.Progress("never-existed"));
    }

    /// <summary>
    /// A run shorter than one poll interval deletes its frame first, so "seen" is never true;
    /// the watcher must still end once the CLI has exited.
    /// </summary>
    [Fact]
    public async System.Threading.Tasks.Task WatchProgressEndsWhenTheRunFinishedWithoutAFrame()
    {
        var frames = 0;
        await foreach (var _ in _store.WatchProgress("run-9", default, hasFinished: () => true))
        {
            frames++;
        }
        Assert.Equal(0, frames);
    }

    [Fact]
    public void RunLockIsOnlyLiveWhileHeld()
    {
        Assert.False(_store.RunInProgress());

        var path = Path.Combine(_dir, "run.lock");
        File.WriteAllText(path, "");
        Assert.False(_store.RunInProgress());

        using (new FileStream(path, FileMode.Open, FileAccess.ReadWrite, FileShare.None))
        {
            Assert.True(_store.RunInProgress());
        }
        Assert.False(_store.RunInProgress());
    }
}
