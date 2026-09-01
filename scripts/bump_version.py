#!/usr/bin/env python3
"""Update every shipped Backer version from one validated release number."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
JOURNAL = ".backer-bump-version.json"
TRANSACTION_DIR = ".backer-bump-version"
LOCK = ".backer-release.lock"
SCHEMA = 1
TARGETS = (
    Path("pyproject.toml"),
    Path("src/backer/_version.py"),
    Path("installer/backer-agent.iss"),
    Path("android/app/build.gradle.kts"),
)


class TransactionError(RuntimeError):
    """A version transaction could not finish or recover."""


class ReleaseLock:
    """Nonblocking process lock, released by the OS when its owner exits."""

    def __enter__(self) -> ReleaseLock:
        identity = hashlib.sha256(str(ROOT.resolve()).encode("utf-8")).hexdigest()
        self.path = Path(tempfile.gettempdir()) / f"{LOCK}-{identity}"
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        if not self.handle.read(1):
            self.handle.seek(0)
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise TransactionError("another bump_version process is active") from exc
        return self

    def __exit__(self, *_: object) -> None:
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()


def fsync_directory(path: Path) -> None:
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
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if metadata is not None:
        os.chmod(path, metadata.st_mode)
        os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    fsync_directory(path.parent)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def replace_once(text: str, pattern: str, replacement: str, path: Path) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"{path}: expected one version field")
    return updated


def version_files(version: str) -> dict[Path, bytes]:
    major, minor, patch = (int(part) for part in version.split("."))
    android_code = major * 10000 + minor * 100 + patch
    rules = (
        (r'^(version\s*=\s*")[^"]+(")(?=\r?$)', rf'\g<1>{version}\g<2>'),
        (r'^(__version__\s*=\s*")[^"]+(")(?=\r?$)', rf'\g<1>{version}\g<2>'),
        (r'^(#define\s+MyAppVersion\s+")[^"]+(")(?=\r?$)', rf'\g<1>{version}\g<2>'),
        (r'^([ \t]*versionCode\s*=\s*)\d+(?=\r?$)', rf'\g<1>{android_code}'),
    )
    updates = {
        ROOT / target: replace_once((ROOT / target).read_bytes().decode("utf-8"), *rule, ROOT / target)
        for target, rule in zip(TARGETS, rules, strict=True)
    }
    android = ROOT / TARGETS[3]
    updates[android] = replace_once(
        updates[android], r'^([ \t]*versionName\s*=\s*")[^"]+(")(?=\r?$)', rf'\g<1>{version}\g<2>', android
    )
    return {path: text.encode("utf-8") for path, text in updates.items()}


def journal_path() -> Path:
    return ROOT / JOURNAL


def transaction_path() -> Path:
    return ROOT / TRANSACTION_DIR


def direct_child(transaction: Path, name: str) -> Path:
    if not isinstance(name, str) or Path(name).name != name or name in {"", ".", ".."}:
        raise TransactionError("invalid transaction artifact name")
    path = transaction / name
    try:
        if path.resolve(strict=True).parent != transaction.resolve(strict=True):
            raise TransactionError("transaction artifact escapes its directory")
    except OSError as exc:
        raise TransactionError(f"missing transaction artifact: {name}") from exc
    if path.is_symlink() or not path.is_file():
        raise TransactionError("transaction artifact is not a regular file")
    return path


def entry_for(index: int, target: Path, backup: Path, staged: Path) -> dict[str, object]:
    backup_data, staged_data = backup.read_bytes(), staged.read_bytes()
    return {
        "target": target.as_posix(),
        "backup": backup.name,
        "staged": staged.name,
        "backup_size": len(backup_data),
        "backup_sha256": digest(backup_data),
        "backup_mode": backup.stat().st_mode & 0o777,
        "staged_size": len(staged_data),
        "staged_sha256": digest(staged_data),
        "staged_mode": staged.stat().st_mode & 0o777,
    }


def write_journal(entries: list[dict[str, object]]) -> None:
    temporary = journal_path().with_suffix(".tmp")
    payload = json.dumps({"schema": SCHEMA, "entries": entries}, sort_keys=True).encode("utf-8")
    write_durable(temporary, payload)
    os.replace(temporary, journal_path())
    fsync_directory(ROOT)


def load_manifest() -> list[tuple[Path, Path, Path]]:
    try:
        payload = json.loads(journal_path().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TransactionError(f"cannot read interrupted version transaction: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema", "entries"} or payload["schema"] != SCHEMA:
        raise TransactionError("invalid transaction journal schema")
    entries = payload["entries"]
    if not isinstance(entries, list) or len(entries) != len(TARGETS):
        raise TransactionError("invalid transaction journal entries")
    transaction = transaction_path()
    expected_keys = {
        "target", "backup", "staged", "backup_size", "backup_sha256", "backup_mode",
        "staged_size", "staged_sha256", "staged_mode",
    }
    manifest: list[tuple[Path, Path, Path]] = []
    for index, (entry, relative_target) in enumerate(zip(entries, TARGETS, strict=True)):
        if not isinstance(entry, dict) or set(entry) != expected_keys:
            raise TransactionError("invalid transaction journal entry")
        if entry["target"] != relative_target.as_posix():
            raise TransactionError("unexpected transaction target")
        if entry["backup"] != f"{index}.original" or entry["staged"] != f"{index}.new":
            raise TransactionError("unexpected transaction artifact names")
        backup, staged = direct_child(transaction, entry["backup"]), direct_child(transaction, entry["staged"])
        for path, prefix in ((backup, "backup"), (staged, "staged")):
            data = path.read_bytes()
            if (
                not isinstance(entry[f"{prefix}_size"], int)
                or entry[f"{prefix}_size"] != len(data)
                or not isinstance(entry[f"{prefix}_sha256"], str)
                or entry[f"{prefix}_sha256"] != digest(data)
                or not isinstance(entry[f"{prefix}_mode"], int)
                or entry[f"{prefix}_mode"] != path.stat().st_mode & 0o777
            ):
                raise TransactionError(f"invalid {prefix} transaction artifact")
        manifest.append((ROOT / relative_target, backup, staged))
    return manifest


def cleanup_transaction() -> None:
    journal_path().unlink(missing_ok=True)
    shutil.rmtree(transaction_path(), ignore_errors=True)
    fsync_directory(ROOT)


def recover_transaction(manifest: list[tuple[Path, Path, Path]]) -> None:
    transaction = transaction_path()
    errors: list[str] = []
    for index, (target, backup, _) in enumerate(manifest):
        try:
            restore = transaction / f"{index}.restore"
            write_durable(restore, backup.read_bytes(), backup.stat())
            os.replace(restore, target)
            fsync_directory(target.parent)
        except OSError as exc:
            errors.append(f"{target}: {exc}")
    if errors:
        raise TransactionError("; ".join(errors))
    write_durable(transaction / ".completed", b"1")
    cleanup_transaction()


def recover_pending() -> None:
    transaction = transaction_path()
    if transaction.is_symlink():
        raise TransactionError("transaction directory must not be a symlink")
    if transaction.exists() and (transaction / ".completed").exists():
        cleanup_transaction()
    elif journal_path().exists():
        recover_transaction(load_manifest())
    elif transaction.exists():
        if (transaction / ".completed").exists():
            shutil.rmtree(transaction, ignore_errors=True)
            fsync_directory(ROOT)
            return
        if (transaction / ".commit-started").exists():
            raise TransactionError("transaction state is incomplete without its journal")
        shutil.rmtree(transaction, ignore_errors=True)
        fsync_directory(ROOT)


def write_all(updates: dict[Path, bytes]) -> None:
    transaction = transaction_path()
    transaction.mkdir()
    try:
        entries: list[dict[str, object]] = []
        for index, relative_target in enumerate(TARGETS):
            path = ROOT / relative_target
            metadata = path.stat()
            backup, staged = transaction / f"{index}.original", transaction / f"{index}.new"
            write_durable(backup, path.read_bytes(), metadata)
            write_durable(staged, updates[path], metadata)
            entries.append(entry_for(index, relative_target, backup, staged))
        write_journal(entries)
        write_durable(transaction / ".commit-started", b"1")
    except Exception:
        if not journal_path().exists():
            shutil.rmtree(transaction, ignore_errors=True)
        raise

    try:
        manifest = load_manifest()
        for index, (target, _, staged) in enumerate(manifest):
            apply = transaction / f"{index}.apply"
            write_durable(apply, staged.read_bytes(), staged.stat())
            os.replace(apply, target)
            fsync_directory(target.parent)
    except Exception as exc:
        try:
            recover_transaction(load_manifest())
        except TransactionError as rollback_error:
            raise TransactionError(f"{exc}; rollback failed: {rollback_error}") from exc
        raise TransactionError(str(exc)) from exc
    write_durable(transaction / ".completed", b"1")
    cleanup_transaction()


def main() -> int:
    try:
        with ReleaseLock():
            recover_pending()
            if len(sys.argv) != 2 or not VERSION.fullmatch(sys.argv[1]):
                print("usage: bump_version.py <major.minor.patch>", file=sys.stderr)
                return 2
            version = sys.argv[1]
            changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
            if not re.search(rf"^## {re.escape(version)}$", changelog, re.MULTILINE):
                print(f"CHANGELOG.md must contain '## {version}' before bumping versions", file=sys.stderr)
                return 2
            write_all(version_files(version))
    except (OSError, TransactionError, ValueError) as exc:
        print(f"could not update release versions: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
