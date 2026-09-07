using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Backer.Desktop.Services;
using Xunit;

namespace Backer.Desktop.Tests;

public sealed class CliRunnerTests : IDisposable
{
    private readonly string _temp = Directory.CreateTempSubdirectory("backer-cli").FullName;

    public void Dispose() => Directory.Delete(_temp, recursive: true);

    /// <summary>A stand-in for the backer CLI. POSIX only; these tests are skipped on Windows.</summary>
    private string FakeCli(string body)
    {
        var path = Path.Combine(_temp, "fake-backer");
        File.WriteAllText(path, "#!/bin/sh\n" + body);
        if (!OperatingSystem.IsWindows())
        {
            File.SetUnixFileMode(
                path,
                UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        }
        return path;
    }

    [Fact]
    public async Task PassesArgumentsVerbatimWithoutAShell()
    {
        var runner = new CliRunner(FakeCli("for a in \"$@\"; do echo \"[$a]\"; done\n"));
        var result = await runner.RunAsync(new[] { "job", "run", "Daily Docs", "$(whoami)", "--json" });

        Assert.True(result.Ok);
        Assert.Equal("[job]\n[run]\n[Daily Docs]\n[$(whoami)]\n[--json]\n", result.Stdout.Replace("\r\n", "\n"));
    }

    [Fact]
    public async Task SecretGoesToStdinAndNeverToArgv()
    {
        var runner = new CliRunner(FakeCli("echo \"argv=$*\"; read secret; echo \"stdin=$secret\"\n"));
        var result = await runner.RunAsync(new[] { "repo", "add", "nas", "--passphrase-stdin" }, stdin: "hunter2\n");

        Assert.Contains("stdin=hunter2", result.Stdout);
        Assert.DoesNotContain("hunter2", result.Stdout.Split('\n')[0]);
    }

    [Fact]
    public async Task StdinIsClosedSoTheCliNeverBlocks()
    {
        var runner = new CliRunner(FakeCli("cat >/dev/null; echo done\n"));
        var result = await runner.RunAsync(new[] { "status" });
        Assert.Contains("done", result.Stdout);
    }

    [Fact]
    public async Task SurfacesExitCodeAndStderr()
    {
        var runner = new CliRunner(FakeCli("echo 'Error: no such job' >&2; exit 2\n"));
        var result = await runner.RunAsync(new[] { "job", "show", "nope" });

        Assert.False(result.Ok);
        Assert.Equal(2, result.ExitCode);
        Assert.Equal("Error: no such job", result.FailureText);
    }

    [Fact]
    public async Task ParsesTheFirstJsonLine()
    {
        var runner = new CliRunner(FakeCli("echo '{\"run_id\": \"20260902T101500Z-1a2b3c4d\"}'; echo '{\"ok\": true}'\n"));
        var result = await runner.RunAsync(new[] { "job", "run", "docs", "--json" });

        var started = result.FirstJsonLine<RunStarted>();
        Assert.Equal("20260902T101500Z-1a2b3c4d", started!.RunId);
    }

    [Fact]
    public async Task StopAllStopsARunningChildSoQuittingCancelsTheBackup()
    {
        if (OperatingSystem.IsWindows())
        {
            return;
        }
        var runner = new CliRunner(FakeCli("sleep 30\n"));
        var run = runner.RunAsync(new[] { "job", "run", "docs" });
        // Let the child actually start before we quit.
        await Task.Delay(300);

        var stopwatch = Stopwatch.StartNew();
        await CliRunner.StopAllAsync();
        var result = await run;
        stopwatch.Stop();

        // The child was stopped by the quit path, so the run ends promptly and unsuccessfully
        // (it does not go through RunAsync's own cancellation, so Cancelled stays false).
        Assert.False(result.Ok);
        Assert.True(stopwatch.Elapsed < TimeSpan.FromSeconds(10), $"took {stopwatch.Elapsed}");
    }

    [Fact]
    public async Task CancellationKillsTheChild()
    {
        var runner = new CliRunner(FakeCli("sleep 30\n"));
        using var cancellation = new CancellationTokenSource(200);
        var stopwatch = Stopwatch.StartNew();

        var result = await runner.RunAsync(new[] { "job", "run", "docs" }, cancellationToken: cancellation.Token);

        stopwatch.Stop();
        Assert.True(result.Cancelled);
        Assert.False(result.Ok);
        Assert.True(stopwatch.Elapsed < TimeSpan.FromSeconds(10), $"took {stopwatch.Elapsed}");
    }

    /// <summary>
    /// Cancel must really take the grandchild with it: a kopia child outliving the CLI would
    /// keep writing to the repository after the GUI said the run had stopped.
    /// </summary>
    [Fact]
    public async Task CancellationKillsTheWholeProcessTree()
    {
        if (OperatingSystem.IsWindows())
        {
            return;
        }
        var pidFile = Path.Combine(_temp, "grandchild.pid");
        var runner = new CliRunner(FakeCli(
            $"sh -c 'sleep 30' & echo $! > {pidFile}\ntrap '' INT\nwait\n"));
        using var cancellation = new CancellationTokenSource();

        var task = runner.RunAsync(new[] { "job", "run", "docs" }, cancellationToken: cancellation.Token);
        while (!File.Exists(pidFile) || File.ReadAllText(pidFile).Trim().Length == 0)
        {
            await Task.Delay(50);
        }
        var grandchild = int.Parse(File.ReadAllText(pidFile).Trim());
        Assert.True(Directory.Exists($"/proc/{grandchild}"), "grandchild never started");

        cancellation.Cancel();
        var result = await task;

        Assert.True(result.Cancelled);
        // The kill is delivered synchronously but reaping is not: give the kernel a moment.
        for (var attempt = 0; attempt < 40 && Directory.Exists($"/proc/{grandchild}"); attempt++)
        {
            await Task.Delay(50);
        }
        Assert.False(Directory.Exists($"/proc/{grandchild}"), $"grandchild {grandchild} survived the cancel");
    }

    /// <summary>SIGINT first: the CLI writes its cancelled run record and deletes the frame.</summary>
    [Fact]
    public async Task CancellationAsksTheChildToStopBeforeKillingIt()
    {
        if (OperatingSystem.IsWindows())
        {
            return;
        }
        var marker = Path.Combine(_temp, "sigint");
        // The background sleep drops the inherited pipes: a pipe holder would keep the parent's
        // output readers open long after it exited, which is not what this test is about.
        var runner = new CliRunner(FakeCli(
            $"trap 'echo caught > {marker}; exit 130' INT\nsleep 30 >/dev/null 2>&1 &\nwait\n"));
        using var cancellation = new CancellationTokenSource();
        var stopwatch = Stopwatch.StartNew();

        var task = runner.RunAsync(new[] { "job", "run", "docs" }, cancellationToken: cancellation.Token);
        await Task.Delay(300);
        cancellation.Cancel();
        var result = await task;
        stopwatch.Stop();

        Assert.True(result.Cancelled);
        Assert.True(File.Exists(marker), "the child was killed without being asked to stop first");
        Assert.True(stopwatch.Elapsed < CliRunner.CancelGrace, $"took {stopwatch.Elapsed}");
    }

    /// <summary>A UsageError makes the CLI exit before it reads stdin; that is a result, not a throw.</summary>
    [Fact]
    public async Task AChildThatExitsBeforeReadingStdinStillReturnsItsError()
    {
        var runner = new CliRunner(FakeCli("echo 'Error: --headless is required' >&2\nexit 2\n"));

        for (var attempt = 0; attempt < 6; attempt++)
        {
            var result = await runner.RunAsync(
                new[] { "repo", "add", "nas", "--passphrase-stdin" },
                stdin: new string('x', 200_000));

            Assert.Equal(2, result.ExitCode);
            Assert.Equal("Error: --headless is required", result.FailureText);
        }
    }

    /// <summary>A generated passphrase lives on stdout, so this failure text can never use it.</summary>
    [Fact]
    public void StderrOnlyFailureTextNeverFallsBackToStdout()
    {
        var killed = new CliResult(137, "correct horse battery staple\n", "", Cancelled: false);
        Assert.Equal("backer exited with code 137", killed.StderrOnlyFailureText);
        Assert.Contains("correct horse", killed.FailureText); // the general one still does

        var normal = new CliResult(1, "noise", "Error: no such repository", Cancelled: false);
        Assert.Equal("Error: no such repository", normal.StderrOnlyFailureText);
    }

    /// <summary>
    /// The CLI's success lines carry U+2713, printed after the mutation is already committed. If
    /// the streams are not pinned to UTF-8 on both ends the frozen Windows backer.exe encodes with
    /// cp1252 and dies there, so a completed action is reported to the user as a failure.
    /// </summary>
    [Fact]
    public async Task NonAsciiOutputRoundTrips()
    {
        var runner = new CliRunner(FakeCli("printf '\\342\\234\\223 Local scheduler removed\\n'\n"));
        var result = await runner.RunAsync(new[] { "agent", "uninstall", "--mode", "local", "--yes" });

        Assert.True(result.Ok);
        Assert.Contains("✓ Local scheduler removed", result.Stdout);
    }

    [Fact]
    public void StartInfoPinsUtf8OnBothEnds()
    {
        var info = new CliRunner("backer").BuildStartInfo(new[] { "agent", "status" });

        Assert.Equal("1", info.Environment["PYTHONUTF8"]);
        foreach (var encoding in new[] { info.StandardOutputEncoding, info.StandardErrorEncoding, info.StandardInputEncoding })
        {
            Assert.Equal(Encoding.UTF8.CodePage, encoding!.CodePage);
            Assert.Empty(encoding.GetPreamble()); // no BOM: the CLI would read it as argv/JSON noise
        }
        // A caller's own environment still wins over nothing else being clobbered.
        var withExtra = new CliRunner("backer").BuildStartInfo(
            new[] { "repo", "add" }, new Dictionary<string, string> { ["BACKER_ENROLLMENT_TOKEN"] = "t" });
        Assert.Equal("1", withExtra.Environment["PYTHONUTF8"]);
        Assert.Equal("t", withExtra.Environment["BACKER_ENROLLMENT_TOKEN"]);
    }

    [Fact]
    public void LocateHonoursTheEnvOverride()
    {
        var previous = Environment.GetEnvironmentVariable("BACKER_CLI");
        try
        {
            Environment.SetEnvironmentVariable("BACKER_CLI", "/opt/backer/bin/backer");
            Assert.Equal("/opt/backer/bin/backer", CliRunner.Locate());
        }
        finally
        {
            Environment.SetEnvironmentVariable("BACKER_CLI", previous);
        }
    }

    private sealed class RunStarted
    {
        public string? RunId { get; set; }
    }
}
