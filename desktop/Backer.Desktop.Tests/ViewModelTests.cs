using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using Backer.Desktop.Services;
using Backer.Desktop.ViewModels;
using Xunit;

namespace Backer.Desktop.Tests;

public sealed class ViewModelTests : IDisposable
{
    private readonly string _temp = Directory.CreateTempSubdirectory("backer-vm").FullName;

    public void Dispose() => Directory.Delete(_temp, recursive: true);

    private AppServices Services(string? cli = null) => new()
    {
        Config = new ConfigStore(Path.Combine("Fixtures", "config.yaml")),
        Data = new DataDirStore(_temp),
        Cli = cli is null ? new CliRunner() : new CliRunner(cli),
    };

    /// <summary>A stand-in for the backer CLI that records its argv. POSIX only.</summary>
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

    [Theory]
    [InlineData("{\"run_id\": \"20240102-030405-abc\"}", "20240102-030405-abc")]
    [InlineData("{\"run_id\": \"x\", \"job\": \"docs\"}", "x")]
    [InlineData("{\"job\": \"docs\"}", null)]
    [InlineData("Starting backup", null)]
    [InlineData("{ not json", null)]
    [InlineData("", null)]
    public void RunIdComesFromTheFirstJsonLine(string line, string? expected) =>
        Assert.Equal(expected, RunViewModel.TryRunId(line));

    [Fact]
    public void HomeRowsComeFromTheConfig()
    {
        var home = new HomeViewModel(Services());
        home.Enter();
        home.Exit();

        Assert.Equal(new[] { "Daily Docs", "scratch" }, home.Jobs.Select(row => row.Job));
        var docs = home.Jobs[0];
        Assert.Equal("/home/matt/docs", docs.Source);
        Assert.Equal("nas", docs.Repository);
        Assert.Equal("9f8e7d6c5b4a", docs.RepositoryId);
        Assert.Equal("0 2 * * *", docs.Schedule);
        Assert.Equal("…", docs.Last);
        Assert.Equal("Manual", home.Jobs[1].Schedule);
        Assert.True(home.ServerManaged);
        Assert.False(home.IsEmpty);
    }

    [Fact]
    public void StaleRefreshResultsAreDiscarded()
    {
        var services = Services();
        services.Post = _ => { }; // This test applies refresh results explicitly.
        var home = new HomeViewModel(services);
        home.Enter();
        var summaries = new List<(string, (string, string))> { ("Daily Docs", ("Success", "1.0 KiB")) };

        home.ApplySummaries(home.Generation - 1, summaries);
        Assert.Equal("…", home.Jobs[0].Last);

        home.ApplySummaries(home.Generation, summaries);
        Assert.Equal("Success", home.Jobs[0].Last);
        Assert.Equal("1.0 KiB", home.Jobs[0].Size);

        home.Exit();
        home.ApplySummaries(home.Generation, new List<(string, (string, string))> { ("Daily Docs", ("Failed", "—")) });
        Assert.Equal("Success", home.Jobs[0].Last);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task RestoreUsesTheNewestSnapshotAndOpensTheCompletedLocation(bool zip)
    {
        if (OperatingSystem.IsWindows())
        {
            return;
        }
        var log = Path.Combine(_temp, "restore-argv.log");
        var restoredPath = Path.Combine(_temp, zip ? "restored.zip" : "original");
        if (zip)
        {
            File.WriteAllText(restoredPath, "archive");
        }
        else
        {
            Directory.CreateDirectory(restoredPath);
        }
        var services = Services(FakeCli(
            "if [ \"$1\" = snapshots ]; then\n"
            + "echo '[{\"full_id\":\"older\",\"timestamp\":\"2024-01-01T12:00:00Z\"},"
            + "{\"full_id\":\"newest\",\"timestamp\":\"2024-02-01T12:00:00Z\"}]'\n"
            + $"else\necho \"$*\" >> '{log}'\necho 'Restore completed to {restoredPath}'\nfi\n"));
        var opened = "";
        services.OpenFolder = path => opened = path;
        services.Confirm = _ => throw new InvalidOperationException("Restore should not require a replacement confirmation");
        var restore = new RestoreViewModel(services) { SaveAsZip = zip, SelectedJobName = "Daily Docs" };
        var deadline = DateTime.UtcNow.AddSeconds(5);
        while ((restore.Busy || restore.SelectedSnapshot is null) && DateTime.UtcNow < deadline)
        {
            await Task.Delay(10);
        }
        Assert.False(restore.Busy);
        Assert.Equal("newest", restore.SelectedSnapshot?.Selector);

        if (zip)
        {
            await restore.RestoreAsync();
            Assert.False(File.Exists(log));
            Assert.Contains("folder", restore.StatusText, StringComparison.OrdinalIgnoreCase);
            restore.Destination = _temp;
        }
        await restore.RestoreAsync();

        var arguments = Assert.Single(File.ReadAllLines(log));
        Assert.Contains("--snapshot newest", arguments);
        Assert.Contains(zip ? "--into ZIP" : "--into ORIGINAL", arguments);
        Assert.DoesNotContain("--yes-replace", arguments);
        Assert.DoesNotContain("--dry-run", arguments);
        Assert.Equal(restoredPath, restore.RestoredPath);
        Assert.Equal($"Restore completed to {restoredPath}", restore.StatusText);
        restore.OpenRestoredLocationCommand.Execute(null);
        Assert.Equal(zip ? _temp : restoredPath, opened);
    }

    [Fact]
    public async Task AVerboseRestoreFailureKeepsTheLogInDetailsAndTheStatusShort()
    {
        if (OperatingSystem.IsWindows())
        {
            return;
        }
        const string failure = "Restoring files: 10%\nRestoring files: 50%\nError: permission denied: /Downloads/report.txt";
        var services = Services(FakeCli(
            "if [ \"$1\" = snapshots ]; then\n"
            + "echo '[{\"full_id\":\"snapshot\"}]'\n"
            + $"else\necho '{failure}' >&2\nexit 1\nfi\n"));
        var restore = new RestoreViewModel(services) { SelectedJobName = "Daily Docs" };
        var deadline = DateTime.UtcNow.AddSeconds(5);
        while ((restore.Busy || restore.SelectedSnapshot is null) && DateTime.UtcNow < deadline)
        {
            await Task.Delay(10);
        }
        Assert.False(restore.Busy);
        Assert.NotNull(restore.SelectedSnapshot);

        await restore.RestoreAsync();

        Assert.True(restore.Failed);
        Assert.Equal(failure, restore.Detail);
        Assert.Equal("Restore failed. See Details for the error.", restore.StatusText);
        Assert.Equal("Failed · " + restore.StatusText, services.Status.Status);
    }

    [Fact]
    public void RestoreDefaultsToOriginalAndOnlyZipPassesADestination()
    {
        var restore = new RestoreViewModel(Services()) { Destination = " /tmp/out " };

        Assert.False(restore.SaveAsZip);
        Assert.Equal(
            new[] { "restore", "--job", "Daily Docs", "--snapshot", "abc123", "--into", "ORIGINAL", "--no-progress" },
            restore.BuildArguments("Daily Docs", "abc123"));

        restore.SaveAsZip = true;
        Assert.Equal(
            new[]
            {
                "restore", "--job", "Daily Docs", "--snapshot", "abc123", "--into", "ZIP", "--no-progress",
                "--destination", "/tmp/out",
            },
            restore.BuildArguments("Daily Docs", "abc123"));
    }

    [Fact]
    public void AProtectedDestinationRefusalOffersTheInlineOverride()
    {
        var restore = new RestoreViewModel(Services()) { Destination = "/etc/nginx" };

        var offered = restore.TryOfferProtectedDestination(
            "Error: Backer will not restore into /etc. Choose a folder outside it instead."
            + " To restore here anyway, re-run with --confirm-destination \"/etc/nginx\".");

        Assert.True(offered);
        Assert.True(restore.ProtectedDestinationOffered);
        // Fail closed: offering never ticks the box, and unticked adds no flag.
        Assert.False(restore.RestoreIntoProtectedFolder);
        Assert.DoesNotContain("--confirm-destination", restore.BuildArguments("Docs", "abc"));

        restore.RestoreIntoProtectedFolder = true;
        var arguments = restore.BuildArguments("Docs", "abc");
        Assert.Contains("--confirm-destination", arguments);
        Assert.Equal("/etc/nginx", arguments[arguments.ToList().IndexOf("--confirm-destination") + 1]);

        // Changing the destination withdraws the offer and the consent.
        restore.Destination = "/tmp/elsewhere";
        Assert.False(restore.ProtectedDestinationOffered);
        Assert.DoesNotContain("--confirm-destination", restore.BuildArguments("Docs", "abc"));
    }

    [Fact]
    public void AnOrdinaryFailureDoesNotOfferTheOverride()
    {
        var restore = new RestoreViewModel(Services());

        Assert.False(restore.TryOfferProtectedDestination("Error: Repository is unreachable"));
        Assert.False(restore.ProtectedDestinationOffered);
    }

    [Fact]
    public void SizesMatchThePythonFormatter()
    {
        Assert.Equal("—", HomeViewModel.HumanSize(0));
        Assert.Equal("512.0 B", HomeViewModel.HumanSize(512));
        Assert.Equal("1.0 KiB", HomeViewModel.HumanSize(1024));
        Assert.Equal("1.5 MiB", HomeViewModel.HumanSize(1024 * 1536));
        Assert.Equal("Never run", HomeViewModel.Summarize(null).Last);
    }
}
