using System;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using Backer.Desktop.Services;
using Backer.Desktop.ViewModels;
using Xunit;

namespace Backer.Desktop.Tests;

public sealed class JobTests : IDisposable
{
    private readonly string _temp = Directory.CreateTempSubdirectory("backer-job").FullName;

    public void Dispose() => Directory.Delete(_temp, recursive: true);

    private string Log => Path.Combine(_temp, "argv.log");

    /// <summary>A stand-in for the backer CLI that records its argv. POSIX only.</summary>
    private string FakeCli(string body = "true\n")
    {
        var path = Path.Combine(_temp, "fake-backer");
        File.WriteAllText(path, $"#!/bin/sh\necho \"$*\" >> {Log}; " + body);
        if (!OperatingSystem.IsWindows())
        {
            File.SetUnixFileMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        }
        return path;
    }

    private AppServices Services(string body = "true\n", string? configFile = null) => new()
    {
        Config = new ConfigStore(configFile ?? Path.Combine("Fixtures", "config.yaml")),
        Data = new DataDirStore(_temp),
        Cli = new CliRunner(FakeCli(body)),
        StateStore = new GuiStateStore(_temp),
        PickFolder = () => Task.FromResult<string?>(_temp),
    };

    /// <summary>Uses the two-repository fixture (nas, usb); nothing is preselected.</summary>
    private JobViewModel Job(string body = "true\n")
    {
        var job = new JobViewModel(Services(body));
        job.OnShown();
        return job;
    }

    private JobViewModel JobWithConfig(string yaml, string body = "true\n")
    {
        var configFile = Path.Combine(_temp, "config.yaml");
        File.WriteAllText(configFile, yaml);
        var job = new JobViewModel(Services(body, configFile));
        job.OnShown();
        return job;
    }

    [Fact]
    public void RepositoriesLoadFromConfigAndNoneIsPreselectedWhenThereAreSeveral()
    {
        var job = Job();

        Assert.Equal(new[] { "nas", "usb" }, job.Repositories.Select(choice => choice.Name));
        Assert.Equal("9f8e7d6c5b4a", job.Repositories[0].Id);
        Assert.Null(job.SelectedRepository);
    }

    [Fact]
    public void TheOnlyRepositoryIsPreselected()
    {
        var job = JobWithConfig(
            "repositories:\n  aabbccddeeff:\n    id: aabbccddeeff\n    name: solo\n    type: local\n    path: /mnt/solo\n");

        Assert.Single(job.Repositories);
        Assert.NotNull(job.SelectedRepository);
        Assert.Equal("aabbccddeeff", job.SelectedRepository!.Id);
    }

    [Fact]
    public void TheArgumentsCarryTheRepositoryScheduleAndRetention()
    {
        var job = Job();
        job.SelectedRepository = job.Repositories.First(choice => choice.Id == "9f8e7d6c5b4a");
        job.JobName = "docs";
        job.Source = "/home/matt/docs";
        job.Cron = "0 2 * * *";
        job.KeepLast = "7";
        job.KeepMonthly = "6";

        Assert.Equal(
            new[]
            {
                "job", "create", "docs", "--source", "/home/matt/docs", "--repo", "9f8e7d6c5b4a",
                "--schedule", "0 2 * * *", "--keep-last", "7", "--keep-monthly", "6",
            },
            job.BuildArguments());
    }

    [Fact]
    public void NoScheduleAndExcludesBecomeFlags()
    {
        var job = Job();
        job.SelectedRepository = job.Repositories.First(choice => choice.Id == "9f8e7d6c5b4a");
        job.JobName = "docs";
        job.Source = "/home/matt/docs";
        job.NoSchedule = true;
        job.Excludes = "*.tmp\n\n  node_modules  \n";

        var arguments = job.BuildArguments();
        Assert.Equal(
            new[]
            {
                "job", "create", "docs", "--source", "/home/matt/docs", "--repo", "9f8e7d6c5b4a",
                "--no-schedule", "--exclude", "*.tmp", "--exclude", "node_modules",
            },
            arguments);
        Assert.DoesNotContain("--schedule", arguments);
    }

    [Fact]
    public async Task WithNoRepositoryItRefusesWithoutSpawningAnything()
    {
        var job = JobWithConfig("agent_id: test\n");
        Assert.Empty(job.Repositories);

        job.JobName = "docs";
        job.Source = "/home/matt/docs";
        await job.CreateAsync();

        Assert.False(File.Exists(Log));
        Assert.Contains("Add a repository first", job.StatusText);
    }

    [Fact]
    public async Task WithNoRepositorySelectedItRefusesWithoutSpawningAnything()
    {
        var job = Job();
        Assert.Null(job.SelectedRepository);

        job.JobName = "docs";
        job.Source = "/home/matt/docs";
        await job.CreateAsync();

        Assert.False(File.Exists(Log));
        Assert.Contains("Choose a repository", job.StatusText);
    }

    [Fact]
    public async Task ASuccessfulCreateNavigatesHome()
    {
        var services = Services("echo 'Backup job created'\n");
        var shell = new MainWindowViewModel(services);
        shell.ShowNewJob();
        var job = shell.Job;
        job.SelectedRepository = job.Repositories.First();
        job.JobName = "docs";
        job.Source = "/home/matt/docs";
        job.NoSchedule = true;

        await job.CreateAsync();

        Assert.IsType<HomeViewModel>(shell.CurrentView);
        Assert.Contains("job create docs", File.ReadAllLines(Log)[0]);
    }
}
