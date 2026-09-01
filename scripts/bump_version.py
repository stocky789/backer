#!/usr/bin/env python3
"""Update release versions in the four shipped metadata files."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = rb"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
SEMVER = re.compile(VERSION + rb"\Z")
TARGETS = (
    (Path("pyproject.toml"), rb'(?m)^(version\s*=\s*")(' + VERSION + rb')(")(\r?)$'),
    (Path("src/backer/_version.py"), rb'(?m)^(__version__\s*=\s*")(' + VERSION + rb')(")(\r?)$'),
    (Path("installer/backer-agent.iss"), rb'(?m)^(#define\s+MyAppVersion\s+")(' + VERSION + rb')(")(\r?)$'),
)
ANDROID = Path("android/app/build.gradle.kts")
ANDROID_CODE = rb"(?m)^(versionCode\s*=\s*)(\d+)(\r?)$"
ANDROID_NAME = rb'(?m)^(versionName\s*=\s*")(' + VERSION + rb')(")(\r?)$'


def one_match(data: bytes, pattern: bytes) -> re.Match[bytes]:
    matches = list(re.finditer(pattern, data))
    if len(matches) != 1:
        raise ValueError("expected one version")
    return matches[0]


def version_code(version: bytes) -> bytes:
    major, minor, patch = (int(part) for part in version.split(b"."))
    return str(major * 10000 + minor * 100 + patch).encode()


def replace(match: re.Match[bytes], data: bytes, version: bytes, suffix: bytes) -> bytes:
    return data[: match.start()] + match.group(1) + version + suffix + data[match.end() :]


def bump(version: bytes) -> dict[Path, bytes]:
    if SEMVER.fullmatch(version) is None:
        raise ValueError("version must be X.Y.Z")
    if re.search(rb"(?m)^## " + re.escape(version) + rb"\r?$", (ROOT / "CHANGELOG.md").read_bytes()) is None:
        raise ValueError("CHANGELOG.md has no matching heading")

    updates: dict[Path, bytes] = {}
    current: bytes | None = None
    for path, pattern in TARGETS:
        data = (ROOT / path).read_bytes()
        match = one_match(data, pattern)
        if current is None:
            current = match.group(2)
        elif match.group(2) != current:
            raise ValueError("version files disagree")
        updates[path] = replace(match, data, version, match.group(3) + match.group(4))

    data = (ROOT / ANDROID).read_bytes()
    code, name = one_match(data, ANDROID_CODE), one_match(data, ANDROID_NAME)
    if name.group(2) != current or code.group(2) != version_code(current or b""):
        raise ValueError("version files disagree")
    data = replace(code, data, version_code(version), code.group(3))
    name = one_match(data, ANDROID_NAME)
    updates[ANDROID] = replace(name, data, version, name.group(3) + name.group(4))
    return updates


def main(args: list[str] | None = None) -> int:
    args = sys.argv[1:] if args is None else args
    if len(args) != 1:
        print("usage: bump_version.py X.Y.Z", file=sys.stderr)
        return 2
    try:
        updates = bump(args[0].encode())
    except (OSError, ValueError) as error:
        print(f"bump_version: {error}", file=sys.stderr)
        return 2
    for path, data in updates.items():
        (ROOT / path).write_bytes(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
