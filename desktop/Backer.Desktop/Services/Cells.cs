using System;
using System.Collections.Generic;

namespace Backer.Desktop.Services;

/// <summary>
/// Mirror of src/backer/serverless/cells.py: only proven platform/storage cells are offered.
/// Kept as a literal here because the GUI must answer before it spawns anything; the
/// Python side owns the contract test that catches a drift between the two lists.
/// </summary>
public static class Cells
{
    private static readonly string[] All = { "local", "smb", "s3" };

    public static IReadOnlyList<string> SupportedRepositoryTypes() =>
        OperatingSystem.IsLinux() || OperatingSystem.IsWindows() ? All : Array.Empty<string>();
}
