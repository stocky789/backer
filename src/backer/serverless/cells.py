"""The serverless support contract, earned by mandatory CI cells."""

from __future__ import annotations

import sys

# Changed only with the matching mandatory CI cells in release-validation.yml.
PROVEN_SERVERLESS_CELLS: frozenset[tuple[str, str]] = frozenset(
    (platform, kind) for platform in ("linux", "win32") for kind in ("local", "smb", "s3")
)


def supported_repository_types(platform: str | None = None) -> tuple[str, ...]:
    platform = platform or sys.platform
    platform = "linux" if platform.startswith("linux") else platform
    return tuple(kind for kind in ("local", "smb", "s3") if (platform, kind) in PROVEN_SERVERLESS_CELLS)
