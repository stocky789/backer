#!/usr/bin/env python3
"""Update every shipped Backer version from one validated release number."""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")


def replace_once(text: str, pattern: str, replacement: str, path: Path) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"{path}: expected one version field")
    return updated


def version_files(version: str) -> dict[Path, str]:
    major, minor, patch = (int(part) for part in version.split("."))
    android_code = major * 10000 + minor * 100 + patch
    targets = {
        ROOT / "pyproject.toml": (r'^(version\s*=\s*")[^"]+(")$', rf'\g<1>{version}\g<2>'),
        ROOT / "src/backer/_version.py": (r'^(__version__\s*=\s*")[^"]+(")$', rf'\g<1>{version}\g<2>'),
        ROOT / "installer/backer-agent.iss": (r'^(#define\s+MyAppVersion\s+")[^"]+(")$', rf'\g<1>{version}\g<2>'),
        ROOT / "android/app/build.gradle.kts": (r'^([ \t]*versionCode\s*=\s*)\d+$', rf'\g<1>{android_code}'),
    }
    updates: dict[Path, str] = {}
    for path, (pattern, replacement) in targets.items():
        text = path.read_text(encoding="utf-8")
        updates[path] = replace_once(text, pattern, replacement, path)
    android_path = ROOT / "android/app/build.gradle.kts"
    updates[android_path] = replace_once(
        updates[android_path],
        r'^([ \t]*versionName\s*=\s*")[^"]+(")$',
        rf'\g<1>{version}\g<2>',
        android_path,
    )
    return updates


def write_all(updates: dict[Path, str]) -> None:
    originals = {path: path.read_bytes() for path in updates}
    temporary: dict[Path, Path] = {}
    replaced: list[Path] = []
    try:
        for path, text in updates.items():
            descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
            temporary[path] = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        for path, temporary_path in temporary.items():
            os.replace(temporary_path, path)
            replaced.append(path)
    except OSError:
        for path in reversed(replaced):
            path.write_bytes(originals[path])
        raise
    finally:
        for temporary_path in temporary.values():
            temporary_path.unlink(missing_ok=True)


def main() -> int:
    if len(sys.argv) != 2 or not VERSION.fullmatch(sys.argv[1]):
        print("usage: bump_version.py <major.minor.patch>", file=sys.stderr)
        return 2
    version = sys.argv[1]
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if not re.search(rf"^## {re.escape(version)}$", changelog, re.MULTILINE):
        print(f"CHANGELOG.md must contain '## {version}' before bumping versions", file=sys.stderr)
        return 2
    try:
        write_all(version_files(version))
    except (OSError, ValueError) as exc:
        print(f"could not update release versions: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
