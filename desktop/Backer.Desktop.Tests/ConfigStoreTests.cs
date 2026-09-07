using System;
using System.IO;
using Backer.Desktop.Services;
using Xunit;

namespace Backer.Desktop.Tests;

public sealed class ConfigStoreTests
{
    private static BackerConfig Load()
    {
        var store = new ConfigStore(Path.Combine(AppContext.BaseDirectory, "Fixtures", "config.yaml"));
        return store.Load();
    }

    [Fact]
    public void ParsesAgentAndServerBlock()
    {
        var config = Load();
        Assert.Equal("1a2b3c4d", config.AgentId);
        Assert.NotNull(config.Server);
        Assert.Equal("http://localhost:8420", config.Server!.ServerUrl);
        Assert.Equal("backer/agent/agent-1/secret", config.Server.ClientSecretRef);
        Assert.Equal(60, config.Server.HeartbeatInterval);
    }

    [Fact]
    public void ParsesRepositoriesKeyedById()
    {
        var config = Load();
        Assert.Equal(2, config.Repositories.Count);
        var nas = config.Repositories["9f8e7d6c5b4a"];
        Assert.Equal("nas", nas.Name);
        Assert.Equal("smb", nas.Type);
        Assert.Equal("kopia", nas.Format);
        Assert.Equal("192.168.1.10", nas.Server);
        Assert.Equal("machine", nas.Scope);
        Assert.Equal("backer/repo/9f8e7d6c5b4a/passphrase", nas.PassphraseRef);
        Assert.Null(nas.Bucket);
        Assert.False(nas.UseExistingSession);
    }

    [Fact]
    public void ParsesJobsKeyedByName()
    {
        var config = Load();
        var job = config.Jobs["Daily Docs"];
        Assert.Equal("9f8e7d6c5b4a", job.Repository);
        Assert.Equal("/home/matt/docs", job.Source!.Path);
        Assert.Equal(new[] { "*.tmp", "node_modules" }, job.Source.Excludes);
        Assert.Equal("0 2 * * *", job.Schedule!.Cron);
        Assert.Equal(7, job.Retention!.KeepLast);
        Assert.Null(job.Retention.KeepYearly);
        Assert.True(job.Enabled);

        var scratch = config.Jobs["scratch"];
        Assert.False(scratch.Enabled);
        Assert.Null(scratch.Schedule);
        Assert.Empty(scratch.Source!.Excludes);
    }

    /// <summary>
    /// The CLI replaces config.yaml atomically (os.replace) while the GUI's watcher re-reads it.
    /// Our handle must permit that (FileShare.Delete) — on Windows a plain File.ReadAllText makes
    /// the CLI's write fail, which drives the settings transaction's fail-closed rollback.
    /// </summary>
    [Fact]
    public void ConfigIsOpenedSoTheCliCanStillReplaceTheFile()
    {
        var directory = Directory.CreateTempSubdirectory("backer-config").FullName;
        try
        {
            var path = Path.Combine(directory, "config.yaml");
            File.WriteAllText(path, "agent_id: before\n");
            var store = new ConfigStore(path);

            using (var reader = new FileStream(
                path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
            {
                // A reader holding the same share mode the store uses must not block a replace.
                var replacement = path + ".tmp";
                File.WriteAllText(replacement, "agent_id: after\n");
                File.Move(replacement, path, overwrite: true);
            }
            Assert.Equal("after", store.Load().AgentId);
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    [Fact]
    public void MissingFileIsAnEmptyConfigNotAnError()
    {
        var config = new ConfigStore(Path.Combine(Path.GetTempPath(), "backer-does-not-exist.yaml")).Load();
        Assert.Empty(config.Jobs);
        Assert.Empty(config.Repositories);
        Assert.Null(config.Server);
    }
}
