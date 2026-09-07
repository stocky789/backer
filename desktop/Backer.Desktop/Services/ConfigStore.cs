using System;
using System.IO;
using System.Threading;
using YamlDotNet.Serialization;
using YamlDotNet.Serialization.NamingConventions;

namespace Backer.Desktop.Services;

/// <summary>Read-only view of config.yaml. The GUI never writes it (D8 contract).</summary>
public sealed class ConfigStore : IDisposable
{
    private static readonly IDeserializer Yaml = new DeserializerBuilder()
        .WithNamingConvention(UnderscoredNamingConvention.Instance)
        .IgnoreUnmatchedProperties()
        .Build();

    private readonly Timer _debounce;
    private FileSystemWatcher? _watcher;

    public ConfigStore(string? configFile = null)
    {
        ConfigFile = configFile ?? BackerPaths.ConfigFile();
        _debounce = new Timer(_ => Changed?.Invoke(this, EventArgs.Empty), null, Timeout.Infinite, Timeout.Infinite);
    }

    public string ConfigFile { get; }

    /// <summary>Raised ~300 ms after the last write to config.yaml.</summary>
    public event EventHandler? Changed;

    public static BackerConfig Parse(string yaml) => Yaml.Deserialize<BackerConfig>(yaml) ?? new BackerConfig();

    /// <summary>Load the config; a missing file is an empty config, not an error.</summary>
    public BackerConfig Load()
    {
        if (!File.Exists(ConfigFile))
        {
            return new BackerConfig();
        }
        // FileShare.Delete is required: the writer replaces this file atomically (os.replace),
        // which Windows refuses while a reader holds a handle without it — and the writer is the
        // CLI mid-transaction. Reading must never be able to fail a config write.
        using var stream = new FileStream(
            ConfigFile, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
        using var reader = new StreamReader(stream);
        return Parse(reader.ReadToEnd());
    }

    public void StartWatching()
    {
        if (_watcher is not null)
        {
            return;
        }
        var directory = Path.GetDirectoryName(ConfigFile);
        if (string.IsNullOrEmpty(directory) || !Directory.Exists(directory))
        {
            return;
        }
        _watcher = new FileSystemWatcher(directory, Path.GetFileName(ConfigFile))
        {
            NotifyFilter = NotifyFilters.LastWrite | NotifyFilters.FileName | NotifyFilters.Size,
            EnableRaisingEvents = true,
        };
        _watcher.Changed += OnFileEvent;
        _watcher.Created += OnFileEvent;
        _watcher.Renamed += OnFileEvent;
        _watcher.Deleted += OnFileEvent;
    }

    private void OnFileEvent(object sender, FileSystemEventArgs e) =>
        _debounce.Change(300, Timeout.Infinite);

    public void Dispose()
    {
        _watcher?.Dispose();
        _watcher = null;
        _debounce.Dispose();
    }
}
