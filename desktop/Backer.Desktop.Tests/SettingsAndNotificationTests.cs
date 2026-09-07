using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using Backer.Desktop.Services;
using Backer.Desktop.ViewModels;
using Xunit;

namespace Backer.Desktop.Tests;

public sealed class SettingsAndNotificationTests : IDisposable
{
    private readonly string _temp = Directory.CreateTempSubdirectory("backer-settings").FullName;

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

    private AppServices Services(string body) => new()
    {
        Config = new ConfigStore(Path.Combine("Fixtures", "config.yaml")),
        Data = new DataDirStore(_temp),
        Cli = new CliRunner(FakeCli(body)),
        StateStore = new GuiStateStore(_temp),
    };

    private void WriteLastAttempt(string job, string status, string runId, bool needsInput = false)
    {
        var directory = Path.Combine(_temp, "last_attempt");
        Directory.CreateDirectory(directory);
        File.WriteAllText(
            Path.Combine(directory, job + ".json"),
            $"{{\"job_name\": \"{job}\", \"run_id\": \"{runId}\", \"status\": \"{status}\", "
            + $"\"needs_input\": {needsInput.ToString().ToLowerInvariant()}}}");
    }

    // ---- pause state mapping -------------------------------------------------

    [Theory]
    [InlineData(false, null, "")]
    [InlineData(true, null, "Paused")]
    [InlineData(true, "2026-09-03T02:00:00Z", "Paused until 2026-09-03T02:00:00Z")]
    public void ThePauseStripFollowsScheduleShow(bool paused, string? until, string expected) =>
        Assert.Equal(expected, SettingsViewModel.PauseLabel(new SchedulePause { Paused = paused, Until = until }));

    [Fact]
    public void AnUnreadablePauseStateIsNeverReportedAsRunning() =>
        Assert.Equal("Pause state unknown", SettingsViewModel.PauseLabel(null));

    [Fact]
    public async Task RefreshReadsPauseScheduleAndKeystoreStateFromTheCli()
    {
        var services = Services(
            "case \"$*\" in\n"
            + "  *'schedule show'*) echo '{\"paused\": true, \"until\": null}';;\n"
            + "  *'schedule status'*) echo '{\"configured\": true, \"platform\": \"linux\", \"method\": \"systemd\","
            + " \"scope\": \"user\", \"enabled\": true, \"active\": false}';;\n"
            + "  *'keystore status'*) echo '{\"backend\": \"file\", \"file_fallback\": true}';;\n"
            + "esac\n");
        var settings = new SettingsViewModel(services);

        await settings.RefreshAsync();

        Assert.True(settings.Paused);
        Assert.Equal("Paused", services.Status.PauseState);
        Assert.True(settings.ScheduleConfigured);
        Assert.Contains("systemd", settings.ScheduleText);
        Assert.Contains("enabled", settings.ScheduleText);
        Assert.True(settings.KeystoreFallback);
        Assert.Contains("protected local files", settings.KeystoreWarning);
    }

    [Fact]
    public async Task TurningOffScheduledBackupsNeedsTheConfirmation()
    {
        var log = Path.Combine(_temp, "argv.log");
        var services = Services($"echo \"$*\" >> {log}\n");
        services.Confirm = _ => Task.FromResult(false);
        var settings = new SettingsViewModel(services);

        await settings.DisableScheduleAsync();
        Assert.False(File.Exists(log));
        Assert.Equal("Scheduled backups were left on.", settings.StatusText);

        services.Confirm = _ => Task.FromResult(true);
        await settings.DisableScheduleAsync();
        Assert.Contains("agent uninstall --mode local --yes", File.ReadAllLines(log)[0]);
    }

    [Fact]
    public async Task TheEnrolmentTokenNeverReachesArgv()
    {
        var log = Path.Combine(_temp, "argv.log");
        var services = Services($"echo \"$*\" >> {log}\n");
        var settings = new SettingsViewModel(services) { ServerUrl = "http://backup-box:8420", EnrollmentToken = "s3cr3t" };

        await settings.ConnectAsync();

        var argv = File.ReadAllLines(log)[0];
        Assert.Equal("agent register --server http://backup-box:8420", argv);
        Assert.Equal("", settings.EnrollmentToken);
    }

    [Fact]
    public async Task RemovingTheAgentServiceIsConfirmedAndNeverDeletesConfigOrData()
    {
        var log = Path.Combine(_temp, "argv.log");
        var services = Services($"echo \"$*\" >> {log}\n");
        services.Confirm = _ => Task.FromResult(false);
        var settings = new SettingsViewModel(services);

        await settings.UninstallServerServiceAsync();
        Assert.False(File.Exists(log));
        Assert.Equal("The agent service was left installed.", settings.StatusText);

        ConfirmRequest? shown = null;
        services.Confirm = request =>
        {
            shown = request;
            return Task.FromResult(true);
        };
        await settings.UninstallServerServiceAsync();

        var argv = File.ReadAllLines(log)[0];
        Assert.Equal("agent uninstall --mode server --service-only --yes", argv);
        Assert.Equal("Remove agent service", shown!.Title);
        Assert.Contains("passphrases", shown.Body); // it names what is NOT removed
    }

    [Fact]
    public async Task AServiceChangeRefusedForPrivilegeAsksForElevationAndKeepsTheCliWording()
    {
        var services = Services("echo 'PermissionError: [Errno 13] Permission denied: /etc/backer' >&2; exit 1\n");
        var settings = new SettingsViewModel(services);

        await settings.InstallServerServiceAsync();

        Assert.StartsWith(SettingsViewModel.ElevationHint, settings.StatusText);
        Assert.Contains("Errno 13", settings.StatusText);
        Assert.StartsWith("Failed", services.Status.Status); // never reported as success
    }

    [Theory]
    [InlineData("Error: Access is denied.", true)]
    [InlineData("[Errno 13] Permission denied: '/etc/backer'", true)]
    [InlineData("This command must be run as Administrator", true)]
    [InlineData("Error: no such job 'daily'", false)]
    public void OnlyPrivilegeFailuresAskForElevation(string failure, bool expected) =>
        Assert.Equal(expected, SettingsViewModel.NeedsElevation(failure));

    [Fact]
    public void PauseDurationsAreComputedHereAndPassedAsIso8601()
    {
        var now = new DateTimeOffset(2026, 9, 2, 23, 30, 0, TimeSpan.FromHours(10));
        Assert.Equal("2026-09-03T00:30:00+10:00", SettingsViewModel.PauseStamp(now.AddHours(1)));
        Assert.Equal("2026-09-03T00:00:00+10:00", SettingsViewModel.NextMidnightStamp(now));
        Assert.Equal(new[] { "schedule", "pause" }, SettingsViewModel.PauseArguments(null));
        Assert.Equal(
            new[] { "schedule", "pause", "--until", "2026-09-03T00:00:00+10:00" },
            SettingsViewModel.PauseArguments("2026-09-03T00:00:00+10:00"));
    }

    [Fact]
    public async Task TheTrayPauseDurationsReachTheCli()
    {
        var log = Path.Combine(_temp, "argv.log");
        var settings = new SettingsViewModel(Services($"echo \"$*\" >> {log}\n"));

        await settings.PauseOneHourAsync();
        await settings.PauseUntilTomorrowAsync();

        var argv = File.ReadAllLines(log).Where(line => line.StartsWith("schedule pause", StringComparison.Ordinal)).ToArray();
        Assert.StartsWith("schedule pause --until ", argv[0]);
        Assert.EndsWith("T00:00:00" + SettingsViewModel.PauseStamp(DateTimeOffset.Now)[^6..], argv[1]);
    }

    [Fact]
    public async Task TheScheduledRunTestShowsItsOutputVerbatimAndLocksTheButton()
    {
        var settings = new SettingsViewModel(Services("echo 'Scheduled run reached the repository'\n"));

        var pending = settings.TestScheduledRunAsync();
        await pending;

        Assert.Equal("Scheduled run reached the repository", settings.ScheduledTestOutput);
        Assert.False(settings.ScheduledTestRunning);

        var failing = new SettingsViewModel(Services("echo 'The scheduled task could not be created' >&2; exit 1\n"));
        await failing.TestScheduledRunAsync();
        Assert.Equal("The scheduled task could not be created", failing.ScheduledTestOutput);
    }

    [Fact]
    public async Task RemovingARepositoryNeedsTheTypedNameAndPassesItOn()
    {
        var log = Path.Combine(_temp, "argv.log");
        var services = Services($"echo \"$*\" >> {log}\n");
        var settings = new SettingsViewModel(services);
        settings.LoadRepositories(services.Config.Load());

        Assert.Equal(new[] { "nas", "usb" }, settings.Repositories.Select(row => row.Name).ToArray());
        settings.SelectedRepository = settings.Repositories.First(row => row.Name == "usb");

        services.Confirm = _ => Task.FromResult(false);
        await settings.RemoveRepositoryAsync();
        Assert.False(File.Exists(log));

        ConfirmRequest? shown = null;
        services.Confirm = request =>
        {
            shown = request;
            return Task.FromResult(true);
        };
        await settings.RemoveRepositoryAsync();

        Assert.Equal("usb", shown!.TypedConfirmation);
        Assert.Equal("repo rm usb --yes --confirm-name usb", File.ReadAllLines(log)[0]);
    }

    [Fact]
    public async Task DeletingSmbRepositoryDataNeedsTheExplicitDeletePhrase()
    {
        var log = Path.Combine(_temp, "argv.log");
        var services = Services($"echo \"$*\" >> {log}\n");
        var settings = new SettingsViewModel(services);
        settings.LoadRepositories(services.Config.Load());
        settings.SelectedRepository = settings.Repositories.First(row => row.Name == "nas");
        ConfirmRequest? shown = null;
        services.Confirm = request =>
        {
            shown = request;
            return Task.FromResult(true);
        };

        await settings.DeleteRepositoryDataAsync();

        Assert.Equal("DELETE nas", shown!.TypedConfirmation);
        Assert.Contains("permanently deleted", shown.Body);
        Assert.Equal("repo destroy nas --yes --confirm-name DELETE nas", File.ReadAllLines(log)[0]);
    }

    [Fact]
    public async Task TheRecoveryRecordNeedsThePlainTextAcknowledgementFirst()
    {
        var log = Path.Combine(_temp, "argv.log");
        var services = Services($"echo \"$*\" >> {log}\n");
        services.PickFolder = () => Task.FromResult<string?>(_temp);
        var settings = new SettingsViewModel(services);
        settings.LoadRepositories(services.Config.Load());
        settings.SelectedRepository = settings.Repositories.First(row => row.Name == "nas");

        Assert.False(settings.CanSaveRecoveryRecord);
        await settings.SaveRecoveryRecordAsync();
        Assert.False(File.Exists(log));

        settings.PlainTextAck = true;
        Assert.True(settings.CanSaveRecoveryRecord);
        await settings.SaveRecoveryRecordAsync();

        Assert.Equal(
            $"repo passphrase nas --passphrase-out {Path.Combine(_temp, "backer-recovery-nas.txt")}",
            File.ReadAllLines(log)[0]);
    }

    [Fact]
    public void FilesRepositoriesDoNotOfferPassphraseRecovery()
    {
        var services = Services("true\n");
        var settings = new SettingsViewModel(services);
        settings.LoadRepositories(new BackerConfig
        {
            Repositories = new Dictionary<string, RepositoryConfig>
            {
                ["files"] = new() { Id = "files", Name = "files", Type = "local", Format = "files", Path = "/backups" },
            },
        });
        settings.SelectedRepository = Assert.Single(settings.Repositories);
        settings.PlainTextAck = true;

        Assert.False(settings.IsEncryptedRepository);
        Assert.False(settings.CanSaveRecoveryRecord);
    }

    [Fact]
    public async Task RemovingFilesRepositoryDoesNotClaimItHasAPassphrase()
    {
        var services = Services("true\n");
        var settings = new SettingsViewModel(services);
        settings.LoadRepositories(new BackerConfig
        {
            Repositories = new Dictionary<string, RepositoryConfig>
            {
                ["files"] = new() { Id = "files", Name = "files", Type = "local", Format = "files" },
            },
        });
        settings.SelectedRepository = Assert.Single(settings.Repositories);
        ConfirmRequest? shown = null;
        services.Confirm = request =>
        {
            shown = request;
            return Task.FromResult(false);
        };

        await settings.RemoveRepositoryAsync();

        Assert.DoesNotContain("passphrase", shown!.Body);
        Assert.Contains("readable", shown.Body);
    }

    // ---- notification policy -------------------------------------------------

    [Fact]
    public void FailuresNotifyOncePerDayAndSuccessOnlyTheFirstTime()
    {
        var state = new GuiState();
        var failed = new JobRun { RunId = "r1", Status = "failed" };
        Assert.True(NotificationService.Allowed(state, "docs", failed, "2026-09-02"));
        NotificationService.Record(state, "docs", failed, "2026-09-02");
        Assert.False(NotificationService.Allowed(state, "docs", new JobRun { RunId = "r2", Status = "failed" }, "2026-09-02"));
        Assert.True(NotificationService.Allowed(state, "docs", new JobRun { RunId = "r2", Status = "failed" }, "2026-09-03"));

        var success = new JobRun { RunId = "r3", Status = "success" };
        Assert.True(NotificationService.Allowed(state, "docs", success, "2026-09-03"));
        NotificationService.Record(state, "docs", success, "2026-09-03");
        Assert.False(NotificationService.Allowed(state, "docs", new JobRun { RunId = "r4", Status = "success" }, "2026-09-04"));
        Assert.Empty(state.Attention); // a success clears the attention flag

        var cancelled = new JobRun { RunId = "r5", Status = "cancelled" };
        Assert.False(NotificationService.Allowed(state, "docs", cancelled, "2026-09-04"));

        var input = new JobRun { RunId = "r6", Status = "failed", NeedsInput = true };
        Assert.True(NotificationService.Allowed(state, "docs", input, "2026-09-04"));
        NotificationService.Record(state, "docs", input, "2026-09-04");
        Assert.False(NotificationService.Allowed(state, "docs", input, "2026-09-04"));
        Assert.True(NotificationService.Allowed(
            state, "docs", new JobRun { RunId = "r7", Status = "failed", NeedsInput = true }, "2026-09-04"));
    }

    [Fact]
    public void ThePolicySurvivesARestartAndDrivesTheTrayTooltip()
    {
        var store = new GuiStateStore(_temp);
        var data = new DataDirStore(_temp);
        WriteLastAttempt("docs", "failed", "run-1");

        var sent = new List<Notification>();
        var first = new NotificationService(store, data) { Notify = sent.Add };
        first.Poll(new[] { "docs" });
        Assert.Single(sent);
        Assert.Equal("Backer - 1 backup needs attention", first.TrayTooltip);

        // A fresh process must not replay it.
        var second = new NotificationService(store, data) { Notify = sent.Add };
        second.Poll(new[] { "docs" });
        Assert.Single(sent);
        Assert.Equal(1, second.AttentionCount);

        second.ClearAttention("docs");
        Assert.Equal("Backer", second.TrayTooltip);
        Assert.Empty(new NotificationService(store, data).State.Attention);
    }

    [Fact]
    public void NeedsInputIsWordedDifferentlyFromAPlainFailure()
    {
        Assert.Equal(
            "Backer needs input",
            NotificationService.Describe("docs", new JobRun { Status = "failed", NeedsInput = true }).Title);
        Assert.Contains(
            "did not run",
            NotificationService.Describe("docs", new JobRun { Status = "failed" }).Body);
        Assert.Contains(
            "first backup",
            NotificationService.Describe("docs", new JobRun { Status = "success" }).Body);
    }

    [Fact]
    public void WithoutADesktopNotifierTheMessageLandsInTheInAppBanner()
    {
        var notifications = new NotificationService(new GuiStateStore(_temp), new DataDirStore(_temp));
        var banner = "";
        notifications.Banner = text => banner = text;

        notifications.DesktopNotifier = _ => false; // Windows, or notify-send missing
        notifications.Deliver(new Notification("docs", "Backer", "docs backup did not run."));
        Assert.Equal("docs backup did not run.", banner);

        banner = "";
        notifications.DesktopNotifier = _ => true; // Linux notify-send took it
        notifications.Deliver(new Notification("docs", "Backer", "docs backup did not run."));
        Assert.Equal("", banner);
    }

    [Fact]
    public void TheCloseToTrayHintIsShownExactlyOnceEver()
    {
        var store = new GuiStateStore(_temp);
        var data = new DataDirStore(_temp);

        Assert.True(new NotificationService(store, data).CloseHintOnce());
        Assert.False(new NotificationService(store, data).CloseHintOnce());
        Assert.False(new NotificationService(store, data).CloseHintOnce());
    }

    /// <summary>
    /// The whole app may confirm exactly six irreversible actions — no more. Two of the six
    /// are parameterised and serve two prompts each (see desktop/README.md), so this pins the
    /// owning members rather than the titles: a count alone cannot notice a swap.
    /// </summary>
    [Fact]
    public void ThereAreExactlySixConfirmationDialogs()
    {
        var sites = new List<string>();
        foreach (var file in ClientSources())
        {
            var lines = File.ReadAllLines(file);
            for (var index = 0; index < lines.Length; index++)
            {
                if (!lines[index].Contains("new ConfirmRequest("))
                {
                    continue;
                }
                sites.Add(Path.GetFileNameWithoutExtension(file) + "." + EnclosingMember(lines, index));
            }
        }

        Assert.Equal(
            new[]
            {
                "HomeViewModel.RemoveAsync",                        // remove a backup job
                "MainWindowViewModel.ConfirmInterruptAsync",        // quit during a run / update installer
                "RestoreViewModel.ConfirmReplaceAsync",             // REPLACE restore, typed REPLACE
                "SettingsViewModel.ConfirmStopAsync",               // turn off schedule / remove agent service
                "SettingsViewModel.DeleteRepositoryDataAsync",      // erase SMB repository, typed DELETE name
                "SettingsViewModel.RemoveRepositoryAsync",          // remove a repository, typed name
            },
            sites.OrderBy(site => site, StringComparer.Ordinal).ToArray());
    }

    // ---- contracts the deleted Tk suite used to own -------------------------

    // The "no hard-coded colours" contract moved to ThemeTests: the design tokens live in
    // one theme file now, so the rule needs exactly one exemption and a wider net.

    /// <summary>No engine control: the client spawns `backer`, never the backup engine itself.
    /// Comments may name it (the cancel path explains what the CLI does with a signal).</summary>
    [Fact]
    public void TheClientNeverDrivesTheBackupEngine()
    {
        var offenders = ClientSources()
            .Concat(ClientFiles("*.axaml"))
            .Where(file => File.ReadAllLines(file)
                .Where(line => !line.TrimStart().StartsWith("//", StringComparison.Ordinal)
                    && !line.TrimStart().StartsWith("<!--", StringComparison.Ordinal))
                .Any(line => line.Contains("kopia", StringComparison.OrdinalIgnoreCase)
                    && !line.Contains("Format", StringComparison.Ordinal)
                    && !line.Contains("_format", StringComparison.Ordinal)
                    && !line.Contains("IsKopia", StringComparison.Ordinal)
                    && !line.Contains("UseKopia", StringComparison.Ordinal)))
            .ToList();
        Assert.Empty(offenders);
    }

    private static string DesktopRoot()
    {
        var root = new DirectoryInfo(AppContext.BaseDirectory);
        while (root is not null && !Directory.Exists(Path.Combine(root.FullName, "Backer.Desktop", "ViewModels")))
        {
            root = root.Parent;
        }
        Assert.NotNull(root);
        return Path.Combine(root!.FullName, "Backer.Desktop");
    }

    private static IEnumerable<string> ClientFiles(string pattern) => Directory
        .EnumerateFiles(DesktopRoot(), pattern, SearchOption.AllDirectories)
        .Where(file => !file.Contains($"{Path.DirectorySeparatorChar}obj{Path.DirectorySeparatorChar}")
            && !file.Contains($"{Path.DirectorySeparatorChar}bin{Path.DirectorySeparatorChar}"));

    private static IEnumerable<string> ClientSources() => ClientFiles("*.cs");

    /// <summary>
    /// The Inno installer's manifest requires administrator and we run asInvoker, so the launch
    /// must go through ShellExecute — CreateProcess can only ever return 740 there, which is how
    /// "Check for updates" ended up never installing anything.
    /// </summary>
    [Fact]
    public void TheUpdateInstallerIsLaunchedInAWayThatCanElevate()
    {
        var info = SettingsViewModel.InstallerStartInfo(@"C:\Temp\backer-agent-setup.exe");

        Assert.True(info.UseShellExecute);
        Assert.Empty(info.ArgumentList); // unsupported with ShellExecute; the flags live in Arguments
        Assert.Equal("/VERYSILENT /SUPPRESSMSGBOXES /NORESTART", info.Arguments);
        Assert.Equal("The update was not installed.", SettingsViewModel.UpdateDeclined);
    }

    /// <summary>Name of the method a source line sits in.</summary>
    private static string EnclosingMember(string[] lines, int index)
    {
        for (var line = index; line >= 0; line--)
        {
            var match = Regex.Match(lines[line], @"^\s*(?:public|private|protected|internal)[^=;]*?\b(\w+)\(");
            if (match.Success)
            {
                return match.Groups[1].Value;
            }
        }
        return "?";
    }
}
