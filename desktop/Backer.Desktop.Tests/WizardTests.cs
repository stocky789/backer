using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using Backer.Desktop.Services;
using Backer.Desktop.ViewModels;
using Xunit;

namespace Backer.Desktop.Tests;

public sealed class WizardTests : IDisposable
{
    private readonly string _temp = Directory.CreateTempSubdirectory("backer-wizard").FullName;

    public void Dispose() => Directory.Delete(_temp, recursive: true);

    private string _log => Path.Combine(_temp, "argv.log");

    /// <summary>Stand-in for the backer CLI that records its argv. POSIX only.</summary>
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

    private RepositoryViewModel Wizard(string body) => new(new AppServices
    {
        Config = new ConfigStore(Path.Combine("Fixtures", "config.yaml")),
        Data = new DataDirStore(_temp),
        Cli = new CliRunner(FakeCli($"echo \"$*\" >> {_log}; " + body)),
        StateStore = new GuiStateStore(_temp),
        PickFolder = () => Task.FromResult<string?>(_temp),
    });

    private string[] Argv() => File.Exists(_log) ? File.ReadAllLines(_log) : Array.Empty<string>();

    [Fact]
    public async Task ASuppliedPassphraseMustMatchItsConfirmationBeforeCreating()
    {
        var wizard = Wizard("echo \"Repository 'docs' saved (keyring, id 9f8e7d6c5b4a)\"\n");
        wizard.RepositoryType = "local";
        wizard.Path = "/mnt/backups/docs";
        wizard.Name = "docs";
        wizard.UseGenerated = false;
        wizard.SuppliedPassphrase = "alpha beta gamma";
        wizard.SuppliedConfirmation = "alpha beta";
        wizard.Step = "passphrase";

        // The two entries do not match yet: no CLI call may happen.
        Assert.False(wizard.CanContinue);
        await wizard.ContinueAsync();
        Assert.Empty(Argv());

        // Once they match, the passphrase is revealed and creation proceeds without any extra gate.
        wizard.SuppliedConfirmation = "alpha beta gamma";
        Assert.True(wizard.ShowReveal);
        Assert.True(wizard.CanContinue);
        await wizard.ContinueAsync();

        var argv = Assert.Single(Argv());
        Assert.Contains("repo add docs --init --headless --type local --path /mnt/backups/docs", argv);
        Assert.Contains("--passphrase-stdin", argv);
        Assert.DoesNotContain("alpha beta gamma", argv);
        Assert.Equal("9f8e7d6c5b4a", wizard.RepositoryId);
        // A supplied passphrase is confirmed up front, so the repository is created and the wizard
        // finishes — no job is created here.
        Assert.Equal("done", wizard.Step);
    }

    [Fact]
    public async Task AGeneratedPassphraseIsRevealedThenFinishes()
    {
        var wizard = Wizard("echo 'correct horse battery staple'; echo \"Repository 'docs' saved (file, id abc123def456)\"\n");
        wizard.RepositoryType = "local";
        wizard.Path = "/mnt/backups/docs";
        wizard.Name = "docs";
        wizard.Step = "passphrase";

        await wizard.ContinueAsync();

        Assert.Contains("--generate-passphrase --print-passphrase", Assert.Single(Argv()));
        Assert.True(wizard.Created);
        Assert.Equal("correct horse battery staple", wizard.RevealedPassphrase);
        Assert.Equal("abc123def456", wizard.RepositoryId);
        Assert.Equal("passphrase", wizard.Step); // paused so the passphrase can be read/saved

        // Finishing from the reveal needs no confirmation and spawns nothing more.
        Assert.True(wizard.CanContinue);
        await wizard.ContinueAsync();
        Assert.Equal("done", wizard.Step);
        Assert.Single(Argv());
    }

    [Fact]
    public void SmbAndS3ArgumentsCarryNoSecrets()
    {
        var wizard = Wizard("true\n");
        wizard.RepositoryType = "smb";
        wizard.Name = "nas";
        wizard.Host = "fileserver";
        wizard.Username = "matt";
        wizard.Password = "hunter2";
        wizard.Domain = "WORK";
        wizard.Shares.Add(new ShareRow { Name = "backups" });
        wizard.SelectedShare = wizard.Shares[0];
        wizard.Path = "docs";

        var smb = wizard.BuildRepoAddArguments();
        Assert.Equal(
            new[]
            {
                "repo", "add", "nas", "--init", "--headless", "--type", "smb",
                "--host", "fileserver", "--share", "backups", "--path", "docs",
                "--username", "matt", "--domain", "WORK",
                "--generate-passphrase", "--print-passphrase",
            },
            smb);

        wizard.RepositoryType = "s3";
        wizard.Bucket = "b";
        wizard.Endpoint = "https://s3.example";
        wizard.Region = "ap-southeast-2";
        wizard.AccessKeyId = "AKIA";
        wizard.SecretKey = "sh h";
        Assert.DoesNotContain("sh h", wizard.BuildRepoAddArguments());
    }

    [Fact]
    public async Task FilesFormatSkipsPassphrasesAndCreatesFromStorageDetails()
    {
        var wizard = Wizard("echo \"Repository 'docs' saved (id 9f8e7d6c5b4a)\"\n");
        wizard.RepositoryType = "local";
        wizard.Format = "files";
        wizard.Path = "/mnt/backups/docs";
        wizard.Name = "docs";
        wizard.Step = "detail";

        await wizard.ContinueAsync();

        var argv = Assert.Single(Argv());
        Assert.Contains("--format files", argv);
        Assert.DoesNotContain("passphrase", argv);
        Assert.Equal("done", wizard.Step);
        Assert.False(wizard.IsPassphraseStep);
    }

    [Fact]
    public async Task FilesFormatRejectsS3BeforeAnyCliCall()
    {
        var wizard = Wizard("true\n");
        wizard.Format = "files";
        wizard.RepositoryType = "s3";
        wizard.Step = "detail";

        await wizard.ContinueAsync();

        Assert.Equal("Unencrypted files repositories support local folders and SMB shares, not S3.", wizard.StatusText);
        Assert.Empty(Argv());
    }

    [Fact]
    public async Task ACreateFailureShowsTheCliWordingAndKeepsNoPartialState()
    {
        var wizard = Wizard("echo 'Repository location is not writable' >&2; exit 1\n");
        wizard.Path = "/mnt/nope";
        wizard.Name = "docs";
        wizard.Step = "passphrase";

        await wizard.ContinueAsync();

        Assert.Equal("Repository location is not writable", wizard.StatusText);
        Assert.False(wizard.Created);
        Assert.Null(wizard.RepositoryId);
        Assert.Equal("passphrase", wizard.Step);
    }

    [Fact]
    public async Task TheRecoveryRecordIsWrittenByTheCliAndOnlyAfterTheAcknowledgement()
    {
        var wizard = Wizard("echo 'Recovery record written'\n");
        wizard.Name = "docs";

        await wizard.SaveRecoveryRecordAsync();
        Assert.Empty(Argv());
        Assert.Contains("acknowledgement", wizard.StatusText);

        wizard.PlainTextAck = true;
        await wizard.SaveRecoveryRecordAsync();
        Assert.Empty(Argv()); // nothing exists to export until the repository is created
    }

    [Fact]
    public async Task AGeneratedPassphraseCanBeSavedAsARecoveryRecord()
    {
        var wizard = Wizard("echo 'correct horse battery staple'; echo \"Repository 'docs' saved (file, id abc123def456)\"\n");
        wizard.Path = "/mnt/backups/docs";
        wizard.Name = "docs";
        wizard.Step = "passphrase";
        await wizard.ContinueAsync();

        Assert.False(wizard.CanSaveRecoveryRecord);
        wizard.PlainTextAck = true;
        Assert.True(wizard.CanSaveRecoveryRecord);

        await wizard.SaveRecoveryRecordAsync();

        Assert.Equal(
            $"repo passphrase docs --passphrase-out {Path.Combine(_temp, "backer-recovery-docs.txt")}",
            Argv()[1]);
    }

    /// <summary>A browse listing with one plain folder and one that is already a repository.</summary>
    private const string BrowseJson =
        "echo '{\"path\":\"\",\"entries\":[" +
        "{\"name\":\"docs\",\"is_dir\":true,\"is_repository\":false}," +
        "{\"name\":\"old\",\"is_dir\":true,\"is_repository\":true}]}'\n";

    private static RepositoryViewModel SmbBrowser(RepositoryViewModel wizard)
    {
        wizard.RepositoryType = "smb";
        wizard.Host = "fileserver";
        wizard.Username = "matt";
        wizard.Password = "hunter2";
        wizard.Domain = "WORK";
        wizard.Shares.Add(new ShareRow { Name = "backups" });
        wizard.SelectedShare = wizard.Shares[0];
        return wizard;
    }

    [Fact]
    public void BrowseArgvCarriesShareAndPathAndNoPassword()
    {
        var wizard = SmbBrowser(Wizard("true\n"));
        wizard.Path = "docs";

        var argv = wizard.BuildBrowseArguments();
        Assert.Equal(
            new[]
            {
                "repo", "browse", "--host", "fileserver", "--share", "backups", "--path", "docs",
                "--username", "matt", "--domain", "WORK", "--password-stdin", "--json",
            },
            argv);
        Assert.DoesNotContain("hunter2", argv);
    }

    [Fact]
    public async Task DescendingUpdatesThePathAndReLists()
    {
        var wizard = SmbBrowser(Wizard(BrowseJson));

        await wizard.BrowseAsync();
        Assert.Equal(2, wizard.Folders.Count);

        await wizard.DescendAsync(wizard.Folders[0]);
        Assert.Equal("docs", wizard.Path);
        Assert.Equal(2, wizard.Folders.Count); // re-listed

        var argv = Argv();
        Assert.Equal(2, argv.Length);
        Assert.DoesNotContain("--path", argv[0]); // root listing omits --path
        Assert.Contains("repo browse --host fileserver --share backups --path docs", argv[1]);
        Assert.DoesNotContain("hunter2", argv[1]);
    }

    [Fact]
    public async Task UpPopsOneSegment()
    {
        var wizard = SmbBrowser(Wizard(BrowseJson));
        wizard.Path = "a/b";

        await wizard.UpAsync();

        Assert.Equal("a", wizard.Path);
    }

    [Fact]
    public async Task ARepositoryFolderSurfacesTheAttachHint()
    {
        var wizard = SmbBrowser(Wizard(BrowseJson));
        await wizard.BrowseAsync();

        Assert.False(wizard.ShowAttachHint);
        await wizard.DescendAsync(wizard.Folders[1]); // "old" is a repository
        Assert.Equal("old", wizard.Path);
        Assert.True(wizard.ShowAttachHint);
    }

    [Fact]
    public void NewFolderAppendsAndSelectsWithoutSpawningMkdir()
    {
        var wizard = SmbBrowser(Wizard(BrowseJson));
        wizard.Name = "nas";
        wizard.Path = "docs";
        wizard.NewFolderName = "2024";

        wizard.AddNewFolder();

        Assert.Equal("docs/2024", wizard.Path);
        Assert.Empty(wizard.NewFolderName);
        Assert.Empty(Argv()); // no CLI call — the folder is made by repo add --init
        Assert.Contains("--share backups --path docs/2024", string.Join(" ", wizard.BuildRepoAddArguments()));
    }

    [Fact]
    public void PassphraseWordsSplitLikeThePythonHelper()
    {
        Assert.Equal(4, RepositoryViewModel.PassphraseWords("correct horse  battery-staple").Count);
        Assert.Empty(RepositoryViewModel.PassphraseWords("   "));
    }
}
