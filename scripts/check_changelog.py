#!/usr/bin/env python3
"""Validate CHANGELOG.md structure.

Every release section must be:

    ## <version>

    ### Major Features / ### Minor Features / ### Bug Fixes

in that order, each present subsection holding at least one bullet, and the
newest section must match the version in pyproject.toml.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
SECTIONS = ["Major Features", "Minor Features", "Bug Fixes"]
VERSION_HEADING = re.compile(r"^## (\d+\.\d+\.\d+)$")


def check(changelog: str, project_version: str) -> list[str]:
    """Return a list of problems; empty means the changelog is valid."""
    errors: list[str] = []
    versions: list[str] = []
    current: str | None = None
    seen: list[str] = []
    bullets = 0
    has_entries = False

    def close_subsection() -> None:
        nonlocal has_entries
        if seen and not bullets:
            errors.append(f"{current}: '### {seen[-1]}' has no entries")
        elif seen and seen[-1] in SECTIONS:
            has_entries = True

    def close_release() -> None:
        if current and not has_entries:
            errors.append(f"{current}: no recognized section with entries")

    for line in changelog.splitlines():
        heading = VERSION_HEADING.match(line)
        if heading:
            close_subsection()
            close_release()
            current, seen, bullets, has_entries = heading.group(1), [], 0, False
            versions.append(current)
        elif line.startswith("## ") and current:
            close_subsection()
            close_release()
            current = None
        elif line.startswith("### ") and current:
            close_subsection()
            name = line.removeprefix("### ").strip()
            bullets = 0
            if name not in SECTIONS:
                errors.append(f"{current}: unknown section '{name}', expected one of {SECTIONS}")
            elif name in seen:
                errors.append(f"{current}: duplicate section '{name}'")
            elif seen and SECTIONS.index(name) < SECTIONS.index(seen[-1]):
                errors.append(f"{current}: '{name}' must come before '{seen[-1]}'")
            seen.append(name)
        elif line.startswith("- ") and current:
            if not seen:
                errors.append(f"{current}: entry outside any section: {line[:60]}")
            bullets += 1

    close_subsection()
    close_release()

    if not versions:
        errors.append("no '## <version>' sections found")
    elif versions[0] != project_version:
        errors.append(
            f"newest section is {versions[0]} but pyproject.toml says {project_version}"
        )
    duplicate_versions = sorted({version for version in versions if versions.count(version) > 1})
    for version in duplicate_versions:
        errors.append(f"duplicate release version '{version}'")

    return errors


def main() -> int:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    errors = check(changelog, version)
    for error in errors:
        print(f"CHANGELOG.md: {error}", file=sys.stderr)
    if errors:
        print("\nSee the format rule at the top of CHANGELOG.md.", file=sys.stderr)
        return 1
    print(f"CHANGELOG.md looks good (newest section: {version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
