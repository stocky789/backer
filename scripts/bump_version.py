#!/usr/bin/env python3
"""Update every shipped Backer version from one validated release number."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
JOURNAL = ".backer-bump-version.json"
TRANSACTION_DIR = ".backer-bump-version"


class TransactionError(RuntimeError):
    """A version transaction could not finish or recover."""


def fsync_directory(path: Path) -> None:
    """Flush directory entries where the platform permits it."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        if os.name != "nt":
            raise
    finally:
        os.close(descriptor)


def write_durable(path: Path, data: bytes, metadata: os.stat_result | None = None) -> None:
    """Write bytes without changing the source file's mode or timestamps."""
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if metadata is not None:
        os.chmod(path, metadata.st_mode)
        os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    fsync_directory(path.parent)


def replace_once(text: str, pattern: str, replacement: str, path: Path) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"{path}: expected one version field")
    return updated


def version_files(version: str) -> dict[Path, bytes]:
    major, minor, patch = (int(part) for part in version.split("."))
    android_code = major * 10000 + minor * 100 + patch
    targets = {
        ROOT / "pyproject.toml": (r'^(version\s*=\s*")[^"]+(")(?=\r?$)', rf'\g<1>{version}\g<2>'),
        ROOT / "src/backer/_version.py": (r'^(__version__\s*=\s*")[^"]+(")(?=\r?$)', rf'\g<1>{version}\g<2>'),
        ROOT / "installer/backer-agent.iss": (
            r'^(#define\s+MyAppVersion\s+")[^"]+(")(?=\r?$)',
            rf'\g<1>{version}\g<2>',
        ),
        ROOT / "android/app/build.gradle.kts": (r'^([ \t]*versionCode\s*=\s*)\d+(?=\r?$)', rf'\g<1>{android_code}'),
    }
    updates: dict[Path, str] = {}
    for path, (pattern, replacement) in targets.items():
        updates[path] = replace_once(path.read_bytes().decode("utf-8"), pattern, replacement, path)
    android_path = ROOT / "android/app/build.gradle.kts"
    updates[android_path] = replace_once(
        updates[android_path],
        r'^([ \t]*versionName\s*=\s*")[^"]+(")(?=\r?$)',
        rf'\g<1>{version}\g<2>',
        android_path,
    )
    return {path: text.encode("utf-8") for path, text in updates.items()}


def journal_path() -> Path:
    return ROOT / JOURNAL


def transaction_path() -> Path:
    return ROOT / TRANSACTION_DIR


def write_journal(entries: list[dict[str, str]]) -> None:
    journal = journal_path()
    temporary = journal.with_suffix(".tmp")
    write_durable(temporary, json.dumps({"entries": entries}, sort_keys=True).encode("utf-8"))
    os.replace(temporary, journal)
    fsync_directory(ROOT)


def load_journal() -> list[dict[str, str]]:
    try:
        entries = json.loads(journal_path().read_text(encoding="utf-8"))["entries"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise TransactionError(f"cannot read interrupted version transaction: {exc}") from exc
    if not isinstance(entries, list) or not entries:
        raise TransactionError("cannot read interrupted version transaction: invalid entries")
    return entries


def cleanup_transaction() -> None:
    journal_path().unlink(missing_ok=True)
    shutil.rmtree(transaction_path(), ignore_errors=True)
    fsync_directory(ROOT)


def recover_transaction(entries: list[dict[str, str]]) -> None:
    transaction = transaction_path()
    target_paths = set(version_files("0.0.0"))
    errors: list[str] = []
    for entry in entries:
        try:
            target = ROOT / entry["target"]
            backup = transaction / entry["backup"]
            if target not in target_paths or not backup.is_file():
                raise ValueError("invalid target or backup")
            restore = transaction / f"{entry['backup']}.restore"
            write_durable(restore, backup.read_bytes(), backup.stat())
            os.replace(restore, target)
            fsync_directory(target.parent)
        except (KeyError, OSError, ValueError) as exc:
            errors.append(f"{entry!r}: {exc}")
    if errors:
        raise TransactionError("; ".join(errors))
    cleanup_transaction()


def recover_pending() -> None:
    if journal_path().exists():
        recover_transaction(load_journal())
    elif transaction_path().exists():
        shutil.rmtree(transaction_path(), ignore_errors=True)
        fsync_directory(ROOT)


def write_all(updates: dict[Path, bytes]) -> None:
    transaction = transaction_path()
    entries: list[dict[str, str]] = []
    transaction.mkdir()
    try:
        for index, (path, data) in enumerate(updates.items()):
            metadata = path.stat()
            backup = transaction / f"{index}.original"
            staged = transaction / f"{index}.new"
            write_durable(backup, path.read_bytes(), metadata)
            write_durable(staged, data, metadata)
            entries.append(
                {
                    "target": str(path.relative_to(ROOT)),
                    "backup": backup.name,
                    "staged": staged.name,
                }
            )
        write_journal(entries)
    except Exception:
        if not journal_path().exists():
            shutil.rmtree(transaction, ignore_errors=True)
        raise

    try:
        for entry in entries:
            target = ROOT / entry["target"]
            os.replace(transaction / entry["staged"], target)
            fsync_directory(target.parent)
    except Exception as exc:
        try:
            recover_transaction(entries)
        except TransactionError as rollback_error:
            raise TransactionError(f"{exc}; rollback failed: {rollback_error}") from exc
        raise TransactionError(str(exc)) from exc
    cleanup_transaction()


def main() -> int:
    try:
        recover_pending()
    except TransactionError as exc:
        print(f"could not recover release versions: {exc}", file=sys.stderr)
        return 1
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
    except (OSError, TransactionError, ValueError) as exc:
        print(f"could not update release versions: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
