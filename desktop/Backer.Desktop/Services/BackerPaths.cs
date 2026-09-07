using System;
using System.IO;
using System.Text.RegularExpressions;

namespace Backer.Desktop.Services;

/// <summary>Byte-compatible port of src/backer/core/paths.py.</summary>
public static class BackerPaths
{
    // Same character class as paths.py:50.
    private static readonly Regex UnsafeJobChars = new("[<>:\"/\\\\|?*\\x00-\\x1f]", RegexOptions.Compiled);

    private static bool IsWindows => OperatingSystem.IsWindows();

    private static string Home => Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);

    private static string Env(string name) => Environment.GetEnvironmentVariable(name) ?? "";

    public static string UserConfigDir()
    {
        if (IsWindows)
        {
            var appData = Env("APPDATA");
            if (appData.Length == 0)
            {
                appData = Path.Combine(Home, "AppData", "Roaming");
            }
            return Path.Combine(appData, "Backer");
        }
        var xdg = Env("XDG_CONFIG_HOME");
        return Path.Combine(xdg.Length > 0 ? xdg : Path.Combine(Home, ".config"), "backer");
    }

    public static string MachineConfigDir()
    {
        if (IsWindows)
        {
            var programData = Env("ProgramData");
            return Path.Combine(programData.Length > 0 ? programData : @"C:\ProgramData", "Backer");
        }
        return "/etc/backer";
    }

    public static string ConfigDir()
    {
        var configured = Env("BACKER_CONFIG_DIR");
        if (configured.Length > 0)
        {
            return configured;
        }
        var userDir = UserConfigDir();
        if (File.Exists(Path.Combine(userDir, "config.yaml")))
        {
            return userDir;
        }
        var machineDir = MachineConfigDir();
        if (File.Exists(Path.Combine(machineDir, "config.yaml")))
        {
            return machineDir;
        }
        return userDir;
    }

    public static string ConfigFile() => Path.Combine(ConfigDir(), "config.yaml");

    public static string DataDir()
    {
        var configured = Env("BACKER_DATA_DIR");
        if (configured.Length > 0)
        {
            return configured;
        }
        if (IsWindows)
        {
            var localAppData = Env("LOCALAPPDATA");
            if (localAppData.Length == 0)
            {
                localAppData = Path.Combine(Home, "AppData", "Local");
            }
            return Path.Combine(localAppData, "Backer");
        }
        var xdg = Env("XDG_DATA_HOME");
        return Path.Combine(xdg.Length > 0 ? xdg : Path.Combine(Home, ".local", "share"), "backer");
    }

    public static string JobSubfolder(string jobName) => UnsafeJobChars.Replace(jobName, "_");
}
