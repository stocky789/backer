using System.Collections.Generic;

namespace Backer.Desktop.Services;

// Read-only mirrors of src/backer/core/config.py. Every field is optional: the
// Python side writes with exclude_none, so absent keys are normal.

public sealed class SourceConfig
{
    public string? Path { get; set; }
    public List<string> Excludes { get; set; } = new();
    public List<string> Includes { get; set; } = new();
}

public sealed class ScheduleConfig
{
    public string? Cron { get; set; }
    public string? Interval { get; set; }
}

public sealed class RetentionConfig
{
    public int? KeepLast { get; set; }
    public int? KeepDaily { get; set; }
    public int? KeepWeekly { get; set; }
    public int? KeepMonthly { get; set; }
    public int? KeepYearly { get; set; }
}

public sealed class ClientConfig
{
    public string? ServerUrl { get; set; }
    public string? ClientId { get; set; }
    public string? ClientSecret { get; set; }
    public string? ClientSecretRef { get; set; }
    public int? HeartbeatInterval { get; set; }
}

public sealed class RepositoryConfig
{
    public string? Id { get; set; }
    public string? Name { get; set; }
    public string? Type { get; set; }
    // Missing from legacy config means the safe, encrypted repository format.
    public string Format { get; set; } = "kopia";
    public string? Path { get; set; }
    public string? Server { get; set; }
    public string? Share { get; set; }
    public string? Username { get; set; }
    public string? Domain { get; set; }
    public string? Bucket { get; set; }
    public string? Prefix { get; set; }
    public string? Endpoint { get; set; }
    public string? Region { get; set; }
    public string? Scope { get; set; }
    public string? UniqueId { get; set; }
    public string? AddedAt { get; set; }
    public string? LastCheckStatus { get; set; }
    public string? LastCheckAt { get; set; }
    public bool UseExistingSession { get; set; }
    public bool? PathStyle { get; set; }
    public string? StoragePasswordRef { get; set; }
    public string? PassphraseRef { get; set; }
}

public sealed class JobConfig
{
    public string? Repository { get; set; }
    public SourceConfig? Source { get; set; }
    public ScheduleConfig? Schedule { get; set; }
    public RetentionConfig? Retention { get; set; }
    public bool Enabled { get; set; } = true;
    public List<string> PreScripts { get; set; } = new();
    public List<string> PostScripts { get; set; } = new();
    public List<string> Tags { get; set; } = new();
}

public sealed class BackerConfig
{
    public string? AgentId { get; set; }
    public ClientConfig? Server { get; set; }
    public Dictionary<string, RepositoryConfig> Repositories { get; set; } = new();
    public Dictionary<string, JobConfig> Jobs { get; set; } = new();
}
