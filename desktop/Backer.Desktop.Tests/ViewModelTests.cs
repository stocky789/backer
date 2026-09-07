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
        var home = new HomeViewModel(Services());
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

    [Fact]
    public async Task ReplaceRestoreOnlyDryRunsUntilTheTypedConfirmation()
    {
        var log = Path.Combine(_temp, "argv.log");
        var services = Services(FakeCli($"echo \"$*\" >> {log}; echo 'Dry run: would restore snapshot abc'\n"));
        services.Confirm = _ => Task.FromResult(false);
        var restore = new RestoreViewModel(services)
        {
            Mode = "REPLACE",
            Destination = "/home/matt/docs",
            SelectedJobName = "Daily Docs",
            SelectedSnapshot = new SnapshotRow { FullId = "abc123" },
        };

        await restore.RestoreAsync();

        var declined = File.ReadAllLines(log);
        Assert.Single(declined);
        Assert.Contains("--dry-run", declined[0]);
        Assert.Contains("the destination is unchanged", restore.StatusText);

        services.Confirm = _ => Task.FromResult(true);
        await restore.RestoreAsync();

        var accepted = File.ReadAllLines(log);
        Assert.Equal(3, accepted.Length);
        Assert.Contains("--dry-run", accepted[1]);
        Assert.DoesNotContain("--dry-run", accepted[2]);
        Assert.Contains("--yes-replace", accepted[2]);
    }

    [Fact]
    public void NewAndMergeRestoresNeverCarryTheReplaceFlag()
    {
        var restore = new RestoreViewModel(Services()) { Destination = "/tmp/out", Include = "reports" };

        var arguments = restore.BuildArguments("Daily Docs", "abc123");
        Assert.Equal(
            new[]
            {
                "restore", "--job", "Daily Docs", "--snapshot", "abc123", "--into", "NEW", "--no-progress",
                "--destination", "/tmp/out", "--include", "reports",
            },
            arguments);
        Assert.DoesNotContain("--yes-replace", arguments);
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
