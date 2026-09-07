using System;
using System.Collections.Generic;
using System.Linq;

namespace Backer.Desktop.Services;

/// <summary>Shared field parsing for the wizard and the job editor.</summary>
public static class JobFields
{
    /// <summary>One exclude pattern per line; blank lines are dropped.</summary>
    public static IReadOnlyList<string> ExcludeLines(string text) => text
        .Replace("\r\n", "\n")
        .Split('\n')
        .Select(line => line.Trim())
        .Where(line => line.Length > 0)
        .ToList();
}

/// <summary>
/// `backer repo passphrase NAME --passphrase-out FILE` writes the passphrase as plain text,
/// so both call sites (wizard and Settings) name the file the same way and both require the
/// plain-text acknowledgement before writing.
/// </summary>
public static class RecoveryRecord
{
    public const string Acknowledgement =
        "I understand this file holds the passphrase in plain text and will keep it somewhere safe.";

    public static string FileName(string repositoryName)
    {
        var safe = new string(repositoryName.Trim()
            .Select(character => char.IsLetterOrDigit(character) || character is '-' or '_' ? character : '_')
            .ToArray());
        return $"backer-recovery-{(safe.Length > 0 ? safe : "repository")}.txt";
    }

    public static IReadOnlyList<string> Arguments(string repositoryName, string file) =>
        new[] { "repo", "passphrase", repositoryName, "--passphrase-out", file };

    /// <summary>Full destination for a picked folder.</summary>
    public static string Destination(string folder, string repositoryName) =>
        System.IO.Path.Combine(folder, FileName(repositoryName));
}
