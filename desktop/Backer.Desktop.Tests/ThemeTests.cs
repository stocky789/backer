using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using Backer.Desktop.ViewModels;
using Xunit;

namespace Backer.Desktop.Tests;

/// <summary>
/// The design system has exactly one place colours are allowed to be written down.
/// Everything else consumes tokens, or light/dark and the theme override drift apart.
/// </summary>
public sealed class ThemeTests
{
    /// <summary>#RGB, #RGBA, #RRGGBB and #AARRGGBB, plus any literal Color attribute.</summary>
    private static readonly Regex Hex = new(@"#([0-9a-fA-F]{3,8})\b|Color=""", RegexOptions.Compiled);

    private const string ThemeFile = "Theme.axaml";

    private static string SourceRoot()
    {
        var directory = new DirectoryInfo(AppContext.BaseDirectory);
        while (directory is not null && !File.Exists(Path.Combine(directory.FullName, "Backer.Desktop.sln")))
        {
            directory = directory.Parent;
        }
        Assert.NotNull(directory);
        return Path.Combine(directory!.FullName, "Backer.Desktop");
    }

    [Fact]
    public void OnlyTheThemeFileNamesAColour()
    {
        var root = SourceRoot();
        var offenders = new List<string>();
        var files = Directory.EnumerateFiles(root, "*.axaml", SearchOption.AllDirectories)
            .Concat(Directory.EnumerateFiles(root, "*.cs", SearchOption.AllDirectories))
            .Where(file => !file.Contains($"{Path.DirectorySeparatorChar}obj{Path.DirectorySeparatorChar}")
                && !file.Contains($"{Path.DirectorySeparatorChar}bin{Path.DirectorySeparatorChar}")
                && Path.GetFileName(file) != ThemeFile);
        foreach (var file in files)
        {
            foreach (Match match in Hex.Matches(File.ReadAllText(file)))
            {
                var digits = match.Groups[1].Value;
                if (digits.Length is 0 or 3 or 4 or 6 or 8)
                {
                    offenders.Add($"{Path.GetFileName(file)}: {match.Value}");
                }
            }
        }
        Assert.Empty(offenders);
        // …and the theme file itself must actually exist, or the scan above proves nothing.
        Assert.True(File.Exists(Path.Combine(root, "Styles", ThemeFile)));
    }

    [Theory]
    [InlineData(0, "just now")]
    [InlineData(30, "30 minutes ago")]
    [InlineData(60, "1 hour ago")]
    [InlineData(60 * 26, "yesterday")]
    [InlineData(60 * 24 * 3, "3 days ago")]
    public void RelativeTimeReadsAsPlainEnglish(int minutesAgo, string expected)
    {
        var now = new DateTimeOffset(2026, 3, 4, 12, 0, 0, TimeSpan.Zero);
        Assert.Equal(expected, HomeViewModel.Relative(now.AddMinutes(-minutesAgo), now));
    }

    [Fact]
    public void TheProtectionBannerFollowsTheRows()
    {
        var home = new HomeViewModel(new Backer.Desktop.Services.AppServices());
        Assert.Equal("No local backups yet", home.ProtectionText);

        home.Jobs.Add(Row("a"));
        home.Jobs.Add(Row("b"));
        Assert.Equal("2 backup jobs set up · nothing has run yet", home.ProtectionText);

        home.Jobs[0].Last = "Success";
        home.Jobs[1].Last = "Success";
        home.LastBackupAt = null;
        Assert.True(home.AllProtected);
        Assert.Equal("All 2 backup jobs protected", home.ProtectionText);

        home.Jobs[1].Last = "Failed";
        Assert.True(home.NeedsAttention);
        Assert.Equal("1 backup needs attention", home.ProtectionText);
    }

    private static JobRow Row(string name) => new()
    {
        Job = name,
        Source = "",
        Repository = "",
        Schedule = "",
        RepositoryId = "",
    };
}
