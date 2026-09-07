using System;
using System.IO;
using Backer.Desktop.Services;
using Xunit;

namespace Backer.Desktop.Tests;

/// <summary>
/// Env vars are process-global, so every env-mutating test lives in this one class
/// (xunit runs tests within a class sequentially).
/// </summary>
public sealed class BackerPathsTests : IDisposable
{
    private readonly string _temp = Directory.CreateTempSubdirectory("backer-paths").FullName;
    private readonly string? _config = Environment.GetEnvironmentVariable("BACKER_CONFIG_DIR");
    private readonly string? _data = Environment.GetEnvironmentVariable("BACKER_DATA_DIR");
    private readonly string? _xdgConfig = Environment.GetEnvironmentVariable("XDG_CONFIG_HOME");
    private readonly string? _xdgData = Environment.GetEnvironmentVariable("XDG_DATA_HOME");

    public void Dispose()
    {
        Environment.SetEnvironmentVariable("BACKER_CONFIG_DIR", _config);
        Environment.SetEnvironmentVariable("BACKER_DATA_DIR", _data);
        Environment.SetEnvironmentVariable("XDG_CONFIG_HOME", _xdgConfig);
        Environment.SetEnvironmentVariable("XDG_DATA_HOME", _xdgData);
        Directory.Delete(_temp, recursive: true);
    }

    [Fact]
    public void ConfigDirHonoursEnvOverride()
    {
        Environment.SetEnvironmentVariable("BACKER_CONFIG_DIR", _temp);
        Assert.Equal(_temp, BackerPaths.ConfigDir());
        Assert.Equal(Path.Combine(_temp, "config.yaml"), BackerPaths.ConfigFile());
    }

    [Fact]
    public void ConfigDirPrefersUserDirWhenConfigExists()
    {
        Environment.SetEnvironmentVariable("BACKER_CONFIG_DIR", null);
        Environment.SetEnvironmentVariable("XDG_CONFIG_HOME", _temp);
        var userDir = Path.Combine(_temp, "backer");
        Assert.Equal(userDir, BackerPaths.ConfigDir());

        Directory.CreateDirectory(userDir);
        File.WriteAllText(Path.Combine(userDir, "config.yaml"), "agent_id: abc\n");
        Assert.Equal(userDir, BackerPaths.ConfigDir());
    }

    [Fact]
    public void DataDirHonoursEnvOverrideThenXdg()
    {
        Environment.SetEnvironmentVariable("BACKER_DATA_DIR", _temp);
        Assert.Equal(_temp, BackerPaths.DataDir());

        Environment.SetEnvironmentVariable("BACKER_DATA_DIR", null);
        Environment.SetEnvironmentVariable("XDG_DATA_HOME", _temp);
        Assert.Equal(Path.Combine(_temp, "backer"), BackerPaths.DataDir());
    }

    /// <summary>
    /// An exported-but-empty XDG variable means "unset", never "the current directory". A
    /// relative config dir would put the GUI and the CLI on different files.
    /// </summary>
    [Fact]
    public void EmptyXdgVariablesBehaveAsUnset()
    {
        if (OperatingSystem.IsWindows())
        {
            return;
        }
        Environment.SetEnvironmentVariable("BACKER_CONFIG_DIR", "");
        Environment.SetEnvironmentVariable("BACKER_DATA_DIR", "");
        Environment.SetEnvironmentVariable("XDG_CONFIG_HOME", "");
        Environment.SetEnvironmentVariable("XDG_DATA_HOME", "");
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);

        Assert.Equal(Path.Combine(home, ".config", "backer"), BackerPaths.UserConfigDir());
        Assert.Equal(Path.Combine(home, ".local", "share", "backer"), BackerPaths.DataDir());
        Assert.True(Path.IsPathRooted(BackerPaths.ConfigDir()));
        Assert.True(Path.IsPathRooted(BackerPaths.DataDir()));
    }

    [Fact]
    public void MachineConfigDirIsEtcBackerOnUnix()
    {
        if (!OperatingSystem.IsWindows())
        {
            Assert.Equal("/etc/backer", BackerPaths.MachineConfigDir());
        }
    }

    [Fact]
    public void JobSubfolderReplacesExactlyThePythonCharacterSet()
    {
        // Character class from src/backer/core/paths.py:50.
        foreach (var unsafeChar in "<>:\"/\\|?*")
        {
            Assert.Equal("a_b", BackerPaths.JobSubfolder($"a{unsafeChar}b"));
        }
        for (var code = 0; code <= 0x1f; code++)
        {
            Assert.Equal("a_b", BackerPaths.JobSubfolder($"a{(char)code}b"));
        }
        Assert.Equal("Daily Docs-2 (v1.2)", BackerPaths.JobSubfolder("Daily Docs-2 (v1.2)"));
        Assert.Equal("C__Users_docs", BackerPaths.JobSubfolder(@"C:\Users/docs"));
    }
}
