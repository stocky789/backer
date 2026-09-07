using System;
using System.Collections.Generic;
using System.IO;
using System.Threading.Tasks;
using Backer.Desktop.Services;
using Backer.Desktop.ViewModels;
using Xunit;

namespace Backer.Desktop.Tests;

/// <summary>StartAsync / progress / stall / Finish — the whole run pipeline.</summary>
public sealed class RunPipelineTests : IDisposable
{
    private readonly string _temp = Directory.CreateTempSubdirectory("backer-run").FullName;

    public void Dispose() => Directory.Delete(_temp, recursive: true);

    private string FakeCli(string body)
    {
        var path = Path.Combine(_temp, "fake-backer");
        File.WriteAllText(path, "#!/bin/sh\n" + body);
        if (!OperatingSystem.IsWindows())
        {
            File.SetUnixFileMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        }
        return path;
    }

    private AppServices Services(string? cli = null) => new()
    {
        Config = new ConfigStore(Path.Combine(AppContext.BaseDirectory, "Fixtures", "config.yaml")),
        Data = new DataDirStore(_temp),
        Cli = cli is null ? new CliRunner() : new CliRunner(cli),
    };

    /// <summary>
    /// A run shorter than one 250 ms poll deletes its progress frame before the watcher sees it.
    /// The watcher must still end, and the run's cancellation source must be cancelled.
    /// </summary>
    [Fact]
    public async Task AShortRunEndsTheProgressWatcherInsteadOfLeakingIt()
    {
        if (OperatingSystem.IsWindows())
        {
            return;
        }
        var services = Services(FakeCli(
            "echo '{\"run_id\": \"r1\"}'; echo '{\"success\": true}'\n"));
        var run = new RunViewModel(services);

        await run.StartAsync("Daily Docs");

        Assert.Equal("Completed", run.State);
        Assert.False(run.IsRunning);

        // The watcher started by the run_id line must terminate on its own: with the frame
        // already gone and the CLI finished, WatchProgress has to break rather than poll forever.
        var frames = 0;
        await foreach (var _ in services.Data.WatchProgress("r1", default, () => true))
        {
            frames++;
        }
        Assert.Equal(0, frames);
    }

    [Fact]
    public async Task ASecondBackUpNowWhileRunningIsANoOp()
    {
        if (OperatingSystem.IsWindows())
        {
            return;
        }
        var log = Path.Combine(_temp, "argv.log");
        var services = Services(FakeCli($"echo \"$*\" >> {log}; sleep 1\n"));
        var run = new RunViewModel(services);

        var first = run.StartAsync("Daily Docs");
        await run.StartAsync("scratch"); // re-entry: must not spawn a second CLI

        Assert.Equal("Daily Docs", run.JobName);
        Assert.Contains("already running", services.Status.Status);
        await first;
        Assert.Single(File.ReadAllLines(log));
    }

    [Fact]
    public void ATransferThatGoesQuietGetsAGentleStillConnectedNote()
    {
        var services = Services();
        var now = DateTimeOffset.UnixEpoch;
        var run = new RunViewModel(services) { Clock = () => now };
        var frame = new ProgressFrame { BytesProcessed = 10, TotalBytes = 100 };

        run.Apply(frame, "");
        Assert.False(run.Stalled);
        Assert.DoesNotContain("still connected", run.State);
        Assert.Contains("10%", run.State); // a real percentage against the total

        now += RunViewModel.StallAfter;
        run.Apply(frame, ""); // same figures, well past the quiet threshold
        Assert.True(run.Stalled);
        Assert.Contains("still connected", run.State);
        Assert.DoesNotContain("stalled", run.State); // never the alarming wording

        run.Apply(new ProgressFrame { BytesProcessed = 20, TotalBytes = 100 }, "");
        Assert.False(run.Stalled);
        Assert.DoesNotContain("still connected", run.State);
        Assert.Contains("20%", run.State);
    }

    [Fact]
    public void TheSnapshotWritePhaseKeepsMovingWhenScanningHasFinished()
    {
        var services = Services();
        var now = DateTimeOffset.UnixEpoch;
        var run = new RunViewModel(services) { Clock = () => now };

        // All bytes scanned; only the snapshot write climbs from here.
        run.Apply(new ProgressFrame { BytesProcessed = 100, TotalBytes = 100, UploadedBytes = 20 }, "");
        now += TimeSpan.FromSeconds(1);
        run.Apply(new ProgressFrame { BytesProcessed = 100, TotalBytes = 100, UploadedBytes = 40 }, "");

        Assert.False(run.Stalled); // uploaded advanced, so it is not "still connected"
        Assert.Contains("writing snapshot", run.State);
        Assert.Contains("/s", run.State); // a live transfer rate from the write delta

        // Writing genuinely stops for over the threshold -> the gentle note appears.
        now += RunViewModel.StallAfter;
        run.Apply(new ProgressFrame { BytesProcessed = 100, TotalBytes = 100, UploadedBytes = 40 }, "");
        Assert.True(run.Stalled);
        Assert.Contains("still connected", run.State);
    }

    [Fact]
    public void TheInitialScanIsNeverReportedAsStalled()
    {
        var services = Services();
        var now = DateTimeOffset.UnixEpoch;
        var run = new RunViewModel(services) { Clock = () => now };

        run.Apply(new ProgressFrame { BytesProcessed = 0 }, ""); // no byte counts yet = scanning
        now += RunViewModel.StallAfter + TimeSpan.FromMinutes(5);
        run.Apply(new ProgressFrame { BytesProcessed = 0 }, ""); // still scanning, long later

        Assert.False(run.Stalled);
        Assert.Contains("Scanning", run.State);
        Assert.DoesNotContain("stall", run.State);
    }

    [Fact]
    public async Task AFailedRunEnablesCopyErrorAndOpenLogFolder()
    {
        if (OperatingSystem.IsWindows())
        {
            return;
        }
        var services = Services(FakeCli("echo 'Error: repository is unreachable' >&2; exit 1\n"));
        var copied = "";
        var opened = "";
        services.CopyText = text => { copied = text; return Task.CompletedTask; };
        services.OpenFolder = path => opened = path;
        var run = new RunViewModel(services);

        await run.StartAsync("Daily Docs");

        Assert.True(run.Failed);
        await run.CopyErrorAsync();
        Assert.Contains("repository is unreachable", copied);
        run.OpenLogFolder();
        Assert.Equal(Path.Combine(_temp, "logs"), opened);

        // A subsequent good run clears the failure actions again.
        services.Cli = new CliRunner(FakeCli("echo '{\"success\": true}'\n"));
        await run.StartAsync("Daily Docs");
        Assert.False(run.Failed);
    }

    /// <summary>Stop must not promise a clean stop on a platform that cannot deliver one.</summary>
    [Fact]
    public void StopWordingMatchesWhatCancelActuallyDoes()
    {
        var wording = CliRunner.StopWording("backup");
        if (OperatingSystem.IsWindows())
        {
            Assert.Contains("ends immediately", wording);
        }
        else
        {
            Assert.Contains("asked to stop cleanly", wording);
            Assert.Contains("forced to stop", wording);
        }
        Assert.DoesNotContain("safely", wording);

        var services = Services();
        var run = new RunViewModel(services);
        run.Stop();
        Assert.Contains(wording, services.Status.Status);

        var restore = new RestoreViewModel(services);
        restore.Stop();
        Assert.Equal(CliRunner.StopWording("restore"), restore.StatusText);
    }

    /// <summary>The timer fires on a pool thread; the UI-bound Jobs list is only read via Post.</summary>
    [Fact]
    public async Task TheBackgroundRefreshMarshalsBeforeReadingTheJobList()
    {
        var services = Services();
        var posted = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        services.Post = action => { posted.TrySetResult(true); action(); };
        var home = new HomeViewModel(services, null);
        home.Enter(); // starts the timer with a zero due time

        var fired = await Task.WhenAny(posted.Task, Task.Delay(TimeSpan.FromSeconds(5)));
        home.Exit();
        Assert.Same(posted.Task, fired);
    }

    /// <summary>An orphaned job has no repository key, and `job rm --repo ""` is not a command.</summary>
    [Fact]
    public async Task RemovingAJobWithNoRepositoryNeverSpawnsTheCli()
    {
        if (OperatingSystem.IsWindows())
        {
            return;
        }
        var log = Path.Combine(_temp, "argv.log");
        var services = Services(FakeCli($"echo \"$*\" >> {log}\n"));
        services.Confirm = _ => Task.FromResult(true);
        var home = new HomeViewModel(services, null);
        home.Enter();
        home.SelectedJob = new JobRow
        {
            Job = "orphan", Source = "/tmp", Repository = "Missing", Schedule = "Manual", RepositoryId = "",
        };

        await home.RemoveAsync();

        Assert.False(File.Exists(log));
        Assert.Contains("has no repository", services.Status.Status);
        home.Exit();
    }

    /// <summary>`agent install --mode local` is refused without an explicit per-platform method.</summary>
    [Fact]
    public void EnablingTheScheduleAlwaysPassesTheMethodTheCliDemands()
    {
        var arguments = SettingsViewModel.EnableScheduleArguments();
        Assert.Equal(
            new[]
            {
                "agent", "install", "--mode", "local", "--method",
                OperatingSystem.IsWindows() ? "task" : "systemd",
            },
            arguments);
    }

    /// <summary>Welcome/Home follow config.yaml when the CLI or the scheduler rewrites it.</summary>
    [Fact]
    public void AConfigChangeSwitchesBetweenWelcomeAndHome()
    {
        var configFile = Path.Combine(_temp, "config.yaml");
        File.WriteAllText(configFile, "jobs: {}\n");
        var services = Services();
        services.Config = new ConfigStore(configFile);
        var shell = new MainWindowViewModel(services);
        shell.Start();
        Assert.IsType<WelcomeViewModel>(shell.CurrentView);

        File.WriteAllText(configFile, "repositories:\n  abc:\n    id: abc\n    name: usb\n    type: local\njobs: {}\n");
        shell.OnConfigChanged();
        Assert.Same(shell.Home, shell.CurrentView);

        File.WriteAllText(configFile, "jobs: {}\n");
        shell.OnConfigChanged();
        Assert.IsType<WelcomeViewModel>(shell.CurrentView);
    }

    /// <summary>
    /// `repo add --generate-passphrase --print-passphrase` puts the passphrase on stdout before
    /// any work. If the child dies with empty stderr, that must not become the status text.
    /// </summary>
    [Fact]
    public async Task AFailedGeneratedPassphraseCreateNeverSurfacesStdout()
    {
        if (OperatingSystem.IsWindows())
        {
            return;
        }
        var services = Services(FakeCli("echo 'correct horse battery staple'\nexit 137\n"));
        var wizard = new RepositoryViewModel(services)
        {
            RepositoryType = "local",
            Name = "usb",
            Path = "/mnt/usb/backer",
            UseGenerated = true,
        };

        await wizard.CreateAsync();

        Assert.DoesNotContain("correct horse", wizard.StatusText);
        Assert.DoesNotContain("correct horse", services.Status.Status);
        Assert.DoesNotContain("correct horse", services.Status.Detail);
        Assert.Contains("137", wizard.StatusText);
    }

    /// <summary>The watcher is wired, so a write to config.yaml raises Changed.</summary>
    [Fact]
    public async Task TheConfigWatcherRaisesChangedOnAWrite()
    {
        var configFile = Path.Combine(_temp, "watched.yaml");
        File.WriteAllText(configFile, "jobs: {}\n");
        using var store = new ConfigStore(configFile);
        var changed = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        store.Changed += (_, _) => changed.TrySetResult(true);
        store.StartWatching();

        File.WriteAllText(configFile, "jobs: {}\nagent_id: abc\n");

        var fired = await Task.WhenAny(changed.Task, Task.Delay(TimeSpan.FromSeconds(10)));
        Assert.Same(changed.Task, fired);
    }
}
