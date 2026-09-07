using System;
using System.IO;
using System.Threading.Tasks;
using Backer.Desktop.Services;
using Backer.Desktop.ViewModels;
using Xunit;

namespace Backer.Desktop.Tests;

public sealed class EditJobTests : IDisposable
{
    private readonly string _temp = Directory.CreateTempSubdirectory("backer-editjob").FullName;

    public void Dispose() => Directory.Delete(_temp, recursive: true);

    private string Log => Path.Combine(_temp, "argv.log");

    private AppServices Services(string body = "true\n")
    {
        var path = Path.Combine(_temp, "fake-backer");
        File.WriteAllText(path, $"#!/bin/sh\necho \"$*\" >> {Log}; " + body);
        if (!OperatingSystem.IsWindows())
        {
            File.SetUnixFileMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        }
        return new AppServices
        {
            Config = new ConfigStore(Path.Combine("Fixtures", "config.yaml")),
            Data = new DataDirStore(_temp),
            Cli = new CliRunner(path),
            StateStore = new GuiStateStore(_temp),
        };
    }

    private EditJobViewModel Editor(string body = "true\n")
    {
        var editor = new EditJobViewModel(Services(body));
        editor.Load("Daily Docs");
        return editor;
    }

    [Fact]
    public void TheFieldsArePreFilledFromConfig()
    {
        var editor = Editor();

        Assert.Equal("/home/matt/docs", editor.SourcePath);
        Assert.Equal("0 2 * * *", editor.Cron);
        Assert.False(editor.NoSchedule);
        Assert.Equal("*.tmp\nnode_modules", editor.Excludes);
        Assert.Equal("7", editor.KeepLast);
        Assert.Equal("30", editor.KeepDaily);
        Assert.Equal("", editor.KeepWeekly);
        Assert.True(editor.Enabled);
    }

    [Fact]
    public void AnUnchangedFormSpawnsNothing()
    {
        var editor = Editor();
        Assert.Empty(editor.BuildArguments());
    }

    [Fact]
    public async Task NothingChangedMeansNoCliCall()
    {
        var editor = Editor();

        await editor.SaveAsync();

        Assert.False(File.Exists(Log));
        Assert.Equal("Nothing was changed.", editor.StatusText);
    }

    [Fact]
    public void OnlyTheChangedFlagsAreSent()
    {
        var editor = Editor();
        editor.Cron = "0 3 * * *";
        editor.KeepLast = "14";
        editor.Excludes = "*.tmp\nnode_modules\n*.iso";
        editor.Enabled = false;

        Assert.Equal(
            new[]
            {
                "job", "set", "Daily Docs", "--schedule", "0 3 * * *", "--keep-last", "14",
                "--exclude", "*.tmp", "--exclude", "node_modules", "--exclude", "*.iso", "--disable",
            },
            editor.BuildArguments());
    }

    [Fact]
    public void ClearingTheExcludesUsesTheClearFlag()
    {
        var editor = Editor();
        editor.Excludes = "  \n";
        Assert.Equal(new[] { "job", "set", "Daily Docs", "--clear-excludes" }, editor.BuildArguments());
    }

    [Fact]
    public void TurningOffTheScheduleUsesNoSchedule()
    {
        var editor = Editor();
        editor.NoSchedule = true;
        Assert.Equal(new[] { "job", "set", "Daily Docs", "--no-schedule" }, editor.BuildArguments());
    }

    [Fact]
    public void ADisabledJobLoadsAsDisabledAndCanBeSwitchedBackOn()
    {
        var editor = new EditJobViewModel(Services());
        editor.Load("scratch");

        Assert.False(editor.Enabled);
        Assert.True(editor.NoSchedule);

        editor.Enabled = true;
        Assert.Equal(new[] { "job", "set", "scratch", "--enable" }, editor.BuildArguments());
    }

    [Fact]
    public async Task ACliRefusalIsShownVerbatim()
    {
        var editor = Editor("echo \"Error: '0 3 * *' is not a five-field cron expression\" >&2; exit 2\n");
        editor.Cron = "0 3 * *";

        await editor.SaveAsync();

        Assert.Equal("Error: '0 3 * *' is not a five-field cron expression", editor.StatusText);
        Assert.False(editor.Busy);
    }

    [Fact]
    public async Task ASuccessfulSaveGoesThroughJobSet()
    {
        var editor = Editor();
        editor.KeepMonthly = "6";

        await editor.SaveAsync();

        Assert.Equal("job set Daily Docs --keep-monthly 6", File.ReadAllLines(Log)[0]);
    }
}
