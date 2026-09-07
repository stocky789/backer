"""Immutable, unencrypted filesystem snapshots."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import mkdtemp
from typing import Any
from uuid import uuid4

from backer.backends.base import BackendBase, BackendResult, BackendType, BackupDestination, BackupSource, OperationType

_FORMAT_VERSION = 1


class FilesBackend(BackendBase):
    """Store each backup as a completed, independently verifiable file tree."""

    backend_type = BackendType.FILES

    def check_available(self) -> tuple[bool, str]:
        return True, "Python filesystem backend"

    @staticmethod
    def _result(
        operation: OperationType, started: datetime, error: Exception | str | None = None, **kwargs: Any
    ) -> BackendResult:
        return BackendResult(
            success=error is None,
            operation=operation,
            started_at=started,
            finished_at=datetime.now(),
            errors=[str(error)] if error else [],
            return_code=0 if error is None else -1,
            **kwargs,
        )

    @staticmethod
    def _safe_relative(value: str) -> PurePosixPath:
        path = PurePosixPath(value.replace("\\", "/"))
        if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError(f"Unsafe relative path: {value!r}")
        return path

    @classmethod
    def _snapshot_id(cls, value: str) -> str:
        path = cls._safe_relative(value)
        if len(path.parts) != 1 or value.startswith(".partial-"):
            raise ValueError(f"Unsafe snapshot ID: {value!r}")
        return path.name

    @staticmethod
    def _is_link(path: Path) -> bool:
        """Reject symlinks plus Windows junctions and other reparse points."""
        if path.is_symlink() or getattr(os.path, "isjunction", lambda _: False)(path):
            return True
        try:
            attributes = os.lstat(path).st_file_attributes
        except (AttributeError, OSError):
            return False
        return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)

    @staticmethod
    def _no_symlink(path: Path) -> None:
        """Reject a path if it or an existing parent is a symlink."""
        current = path.absolute()
        while True:
            if FilesBackend._is_link(current):
                raise ValueError(f"Symlinks are not supported: {current}")
            if current.parent == current:
                return
            current = current.parent

    @staticmethod
    def _contained(path: Path, parent: Path) -> Path:
        try:
            path.relative_to(parent)
        except ValueError as error:
            raise ValueError(f"Path escapes its allowed directory: {path}") from error
        return path

    def _repository_root(self, destination: BackupDestination) -> tuple[Path, Path, str]:
        job_root = Path(destination.path).expanduser().absolute()
        self._no_symlink(job_root)
        # Runner destinations are always <repository>/Agents/<safe-job>.
        if job_root.parent.name != "Agents" or not job_root.name:
            raise ValueError("Files snapshots require a destination beneath Agents/<job>")
        repo = job_root.parent.parent
        self._no_symlink(repo)
        return repo, job_root, job_root.name

    @staticmethod
    def _marker(repo: Path) -> Path:
        return repo / ".backer" / "repository.json"

    def _read_marker(self, repo: Path) -> dict[str, Any]:
        marker = self._marker(repo)
        self._no_symlink(marker)
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Files repository marker is missing or invalid: {marker}") from error
        if (
            not isinstance(value, dict)
            or value.get("format") != "files"
            or value.get("format_version") != _FORMAT_VERSION
        ):
            raise ValueError("Repository format marker is incompatible with files format v1")
        return value

    def _repository_marker(self, repo: Path) -> dict[str, Any]:
        marker = self._read_marker(repo)
        configured_id = self.config.get("repository_id")
        if configured_id and configured_id != marker.get("repository_id"):
            raise ValueError("Repository ID does not match the files repository marker")
        return marker

    def repository_probe(self, path: str) -> tuple[str, str | None]:
        """Kopia-compatible lifecycle probe: present, absent, or unreachable."""
        try:
            repo = Path(path).expanduser().absolute()
            if not repo.exists() or not self._marker(repo).exists():
                return "absent", None
            marker = self._repository_marker(repo)
            return "present", str(marker.get("repository_id") or "") or None
        except (OSError, ValueError):
            return "unreachable", None

    def init_repo(self, destination: BackupDestination) -> BackendResult:
        started = datetime.now()
        try:
            repo = Path(destination.path).expanduser().absolute()
            self._no_symlink(repo)
            if repo.exists() and not repo.is_dir():
                raise ValueError("Files repository path is not a directory")
            if repo.exists() and any(repo.iterdir()):
                raise ValueError("Files repository destination must be empty")
            repo.mkdir(parents=True, exist_ok=True)
            marker = self._marker(repo)
            marker.parent.mkdir(mode=0o700, exist_ok=True)
            marker.write_text(
                json.dumps(
                    {
                        "repository_id": str(self.config.get("repository_id") or uuid4()),
                        "format": "files",
                        "format_version": _FORMAT_VERSION,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            return self._result(OperationType.BACKUP, started)
        except (OSError, ValueError) as error:
            return self._result(OperationType.BACKUP, started, error)

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _matches(rel: PurePosixPath, patterns: list[str]) -> bool:
        name = rel.as_posix()
        return any(fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel.name, pattern) for pattern in patterns)

    def _source_files(self, source: BackupSource, repo: Path) -> list[tuple[Path, PurePosixPath]]:
        root = source.path.expanduser().absolute()
        self._no_symlink(root)
        if not root.exists():
            raise FileNotFoundError(f"Source path does not exist: {root}")
        if root == root.parent:
            raise ValueError("Backing up a filesystem root is not supported")
        if os.path.commonpath([str(root), str(repo)]) in {str(root), str(repo)}:
            raise ValueError("Source and repository must not overlap")
        if root.is_file():
            if not self._is_link(root):
                return [(root, PurePosixPath(root.name))]
            raise ValueError(f"Symlinks are not supported: {root}")
        if not root.is_dir():
            raise ValueError("Source must be a regular file or directory")
        files: list[tuple[Path, PurePosixPath]] = []
        for parent, dirs, names in os.walk(root, followlinks=False):
            parent_path = Path(parent)
            for name in list(dirs):
                entry = parent_path / name
                if self._is_link(entry):
                    raise ValueError(f"Symlinks are not supported: {entry}")
            for name in names:
                entry = parent_path / name
                if self._is_link(entry):
                    raise ValueError(f"Symlinks are not supported: {entry}")
                if not entry.is_file():
                    raise ValueError(f"Only regular files are supported: {entry}")
                rel = PurePosixPath(entry.relative_to(root).as_posix())
                if self._matches(rel, source.excludes) or (source.includes and not self._matches(rel, source.includes)):
                    continue
                files.append((entry, rel))
        return files

    @staticmethod
    def _fsync_file(path: Path) -> None:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        """Best-effort directory durability; Windows does not support opening directories."""
        if os.name == "nt":
            return
        try:
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass

    def _cancelled(self) -> bool:
        event = self.config.get("cancel_event")
        return bool(event and event.is_set())

    def backup(
        self,
        source: BackupSource,
        destination: BackupDestination,
        dry_run: bool = False,
        progress_callback: Any | None = None,
    ) -> BackendResult:
        started = datetime.now()
        staging: Path | None = None
        try:
            if self._cancelled():
                raise InterruptedError("Backup cancelled")
            repo, job_root, job_name = self._repository_root(destination)
            self._repository_marker(repo)
            # Refuse a lossy safe-name collision before adding another job's data.
            if self.config.get("job_name"):
                self.list_snapshots(destination)
            files = self._source_files(source, repo)
            snapshot_id = str(self.config.get("snapshot_id") or self.config.get("run_id") or uuid4())
            snapshot_id = self._snapshot_id(snapshot_id)
            snapshots = job_root / "snapshots"
            self._no_symlink(snapshots)
            target = snapshots / snapshot_id
            self._contained(target, snapshots)
            if dry_run:
                return self._result(
                    OperationType.BACKUP,
                    started,
                    bytes_transferred=sum(item.stat().st_size for item, _ in files),
                    files_transferred=len(files),
                    metadata={"snapshot_id": snapshot_id},
                )
            snapshots.mkdir(parents=True, exist_ok=True)
            staging = Path(mkdtemp(prefix=".partial-", dir=snapshots))
            contents = staging / "contents"
            records: list[dict[str, Any]] = []
            done = 0
            total = sum(item.stat().st_size for item, _ in files)
            for item, rel in files:
                if self._cancelled():
                    raise InterruptedError("Backup cancelled")
                output = contents.joinpath(*rel.parts)
                self._contained(output, contents)
                output.parent.mkdir(parents=True, exist_ok=True)
                stat = item.stat()
                shutil.copy2(item, output, follow_symlinks=False)
                digest = self._hash(output)
                size = output.stat().st_size
                if size != stat.st_size:
                    raise OSError(f"Source changed while being copied: {item}")
                self._fsync_file(output)
                records.append(
                    {
                        "path": rel.as_posix(),
                        "sha256": digest,
                        "size": size,
                        "mtime_ns": stat.st_mtime_ns,
                        "mode": stat.st_mode & 0o777,
                    }
                )
                done += size
                if progress_callback:
                    progress_callback(
                        bytes_done=done,
                        total_bytes=total,
                        files_done=len(records),
                        total_files=len(files),
                        current_file=rel.as_posix(),
                    )
            manifest = {
                "format_version": _FORMAT_VERSION,
                "snapshot_id": snapshot_id,
                "job": job_name,
                "job_identity": str(self.config.get("job_name") or job_name),
                "source_path": str(self.config.get("source_path") or source.path),
                "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "file_count": len(records),
                "bytes": done,
                "files": records,
            }
            manifest_path = staging / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
            )
            self._fsync_file(manifest_path)
            self._fsync_dir(staging)
            if target.exists():
                existing = self._manifest(target, job_name)
                if existing.get("files") != records or existing.get("source_path") != str(
                    self.config.get("source_path") or source.path
                ):
                    raise ValueError(f"Snapshot ID {snapshot_id!r} already exists with different content")
                shutil.rmtree(staging)
            else:
                os.replace(staging, target)
                self._fsync_dir(snapshots)
            return self._result(
                OperationType.BACKUP,
                started,
                bytes_transferred=done,
                files_transferred=len(records),
                metadata={"snapshot_id": snapshot_id, "full_id": snapshot_id},
            )
        except (OSError, ValueError, InterruptedError) as error:
            return self._result(OperationType.BACKUP, started, error)
        finally:
            if staging and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def _manifest(self, snapshot: Path, job_name: str) -> dict[str, Any]:
        self._no_symlink(snapshot)
        try:
            manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid snapshot manifest: {snapshot}") from error
        if (
            not isinstance(manifest, dict)
            or manifest.get("format_version") != _FORMAT_VERSION
            or manifest.get("job") != job_name
            or manifest.get("snapshot_id") != snapshot.name
        ):
            raise ValueError(f"Snapshot does not belong to this job: {snapshot.name}")
        configured_job = self.config.get("job_name")
        if configured_job and manifest.get("job_identity") != configured_job:
            raise ValueError(f"Snapshot job identity does not match this job: {snapshot.name}")
        rows = manifest.get("files")
        if not isinstance(rows, list):
            raise ValueError(f"Invalid snapshot manifest: {snapshot}")
        for row in rows:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("size"), int)
                or not isinstance(row.get("sha256"), str)
            ):
                raise ValueError(f"Invalid snapshot manifest: {snapshot}")
            self._safe_relative(str(row.get("path", "")))
        return manifest

    def list_snapshots(self, destination: BackupDestination) -> list[dict[str, Any]]:
        repo, job_root, job_name = self._repository_root(destination)
        self._repository_marker(repo)
        root = job_root / "snapshots"
        if not root.exists():
            return []
        self._no_symlink(root)
        result = []
        for snapshot in root.iterdir():
            if not snapshot.is_dir() or self._is_link(snapshot) or snapshot.name.startswith(".partial-"):
                continue
            try:
                manifest = self._manifest(snapshot, job_name)
            except ValueError as error:
                if self.config.get("job_name") and "job identity" in str(error):
                    raise
                continue
            result.append(
                {
                    "id": snapshot.name,
                    "full_id": snapshot.name,
                    "timestamp": manifest.get("created_at"),
                    "paths": [manifest.get("source_path")],
                    "size": manifest.get("bytes", 0),
                }
            )
        return sorted(result, key=lambda item: str(item["timestamp"] or ""), reverse=True)

    def _verify_snapshot(self, snapshot: Path, job_name: str) -> dict[str, Any]:
        """Return a manifest only after every listed file is hash-valid."""
        manifest = self._manifest(snapshot, job_name)
        contents = snapshot / "contents"
        self._no_symlink(contents)
        for item in manifest["files"]:
            file = contents / self._safe_relative(item["path"])
            self._contained(file, contents)
            self._no_symlink(file)
            if not file.is_file() or file.stat().st_size != item["size"] or self._hash(file) != item["sha256"]:
                raise ValueError(f"Snapshot file failed integrity check: {item['path']}")
        return manifest

    def restore(
        self,
        source: BackupDestination,
        destination: Path,
        snapshot: str | None = None,
        dry_run: bool = False,
        progress_callback: Any | None = None,
        original_source_path: str | None = None,
        include_path: str | None = None,
    ) -> BackendResult:
        started = datetime.now()
        staging: Path | None = None
        try:
            if self._cancelled():
                raise InterruptedError("Restore cancelled")
            repo, job_root, job_name = self._repository_root(source)
            self._repository_marker(repo)
            rows = self.list_snapshots(source)
            selected = snapshot or (rows[0]["full_id"] if rows else None)
            if not selected:
                raise ValueError("No completed snapshots are available")
            snapshot_path = job_root / "snapshots" / self._snapshot_id(str(selected))
            manifest = self._manifest(snapshot_path, job_name)
            include = self._safe_relative(include_path) if include_path else None
            destination = destination.expanduser().absolute()
            self._no_symlink(destination)
            if destination.exists() and not destination.is_dir():
                raise ValueError("Restore destination is not a directory")
            if os.path.commonpath([str(repo), str(destination)]) in {str(repo), str(destination)}:
                raise ValueError("Repository and restore destination must not overlap")
            selected_rows = [
                row
                for row in manifest["files"]
                if not include or PurePosixPath(row["path"]) == include or include in PurePosixPath(row["path"]).parents
            ]
            prepared: list[tuple[dict[str, Any], PurePosixPath, Path, Path]] = []
            for row in selected_rows:
                rel = self._safe_relative(row["path"])
                input_file = snapshot_path / "contents" / rel
                output = destination.joinpath(*rel.parts)
                self._contained(input_file, snapshot_path / "contents")
                self._contained(output, destination)
                self._no_symlink(input_file)
                self._no_symlink(output)
                if (
                    not input_file.is_file()
                    or self._hash(input_file) != row["sha256"]
                    or input_file.stat().st_size != row["size"]
                ):
                    raise ValueError(f"Snapshot file failed integrity check: {rel}")
                if output.exists() and not output.is_file():
                    raise ValueError(f"Restore output is not a regular file: {output}")
                prepared.append((row, rel, input_file, output))
            if dry_run:
                return self._result(
                    OperationType.RESTORE,
                    started,
                    bytes_transferred=sum(row["size"] for row in selected_rows),
                    files_transferred=len(selected_rows),
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._no_symlink(destination.parent)
            staging = Path(mkdtemp(prefix=".backer-restore-", dir=destination.parent))
            staged_files = staging / "files"
            originals = staging / "originals"
            for row, rel, input_file, _output in prepared:
                if self._cancelled():
                    raise InterruptedError("Restore cancelled")
                staged = staged_files.joinpath(*rel.parts)
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(input_file, staged, follow_symlinks=False)
                os.chmod(staged, row.get("mode", 0o600))
                os.utime(
                    staged,
                    ns=(row.get("mtime_ns", staged.stat().st_mtime_ns), row.get("mtime_ns", staged.stat().st_mtime_ns)),
                )
                self._fsync_file(staged)
            self._fsync_dir(staged_files)

            done = 0
            total = sum(item["size"] for item in selected_rows)
            committed: list[tuple[Path, Path | None]] = []
            created_dirs: list[Path] = []
            try:
                for index, (row, rel, _input_file, output) in enumerate(prepared, 1):
                    if self._cancelled():
                        raise InterruptedError("Restore cancelled")
                    staged = staged_files.joinpath(*rel.parts)
                    original = originals.joinpath(*rel.parts) if output.exists() else None
                    missing_parents = []
                    parent = output.parent
                    while parent != destination.parent and not parent.exists():
                        missing_parents.append(parent)
                        parent = parent.parent
                    output.parent.mkdir(parents=True, exist_ok=True)
                    created_dirs.extend(reversed(missing_parents))
                    self._no_symlink(output)
                    if output.exists() and not output.is_file():
                        raise ValueError(f"Restore output is not a regular file: {output}")
                    if original is not None:
                        original.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(output, original)
                    committed.append((output, original))
                    os.replace(staged, output)
                    done += row["size"]
                    if progress_callback:
                        progress_callback(
                            bytes_done=done,
                            total_bytes=total,
                            files_done=index,
                            total_files=len(selected_rows),
                            current_file=rel.as_posix(),
                        )
            except BaseException:
                rollback_errors = []
                for output, original in reversed(committed):
                    try:
                        output.unlink(missing_ok=True)
                        if original is not None and original.exists():
                            os.replace(original, output)
                    except OSError as error:
                        rollback_errors.append(str(error))
                for directory in reversed(created_dirs):
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
                if rollback_errors:
                    raise OSError("Restore rollback failed: " + "; ".join(rollback_errors))
                raise
            return self._result(
                OperationType.RESTORE,
                started,
                bytes_transferred=done,
                files_transferred=len(selected_rows),
                metadata={"snapshot_id": selected},
            )
        except (OSError, ValueError, InterruptedError) as error:
            return self._result(OperationType.RESTORE, started, error)
        finally:
            if staging and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def check(self, destination: BackupDestination) -> BackendResult:
        started = datetime.now()
        try:
            repo, job_root, job_name = self._repository_root(destination)
            self._repository_marker(repo)
            checked = 0
            bytes_checked = 0
            snapshots = job_root / "snapshots"
            if not snapshots.exists():
                return self._result(OperationType.CHECK, started)
            self._no_symlink(snapshots)
            for snapshot in snapshots.iterdir():
                if not snapshot.is_dir() or self._is_link(snapshot) or snapshot.name.startswith(".partial-"):
                    continue
                manifest = self._verify_snapshot(snapshot, job_name)
                checked += len(manifest["files"])
                bytes_checked += sum(item["size"] for item in manifest["files"])
            return self._result(
                OperationType.CHECK, started, files_transferred=checked, bytes_transferred=bytes_checked
            )
        except (OSError, ValueError) as error:
            return self._result(OperationType.CHECK, started, error)

    def prune(
        self,
        destination: BackupDestination,
        keep_last: int | None = None,
        keep_daily: int | None = None,
        keep_weekly: int | None = None,
        keep_monthly: int | None = None,
        keep_yearly: int | None = None,
        dry_run: bool = False,
        source_path: str | None = None,
    ) -> BackendResult:
        started = datetime.now()
        policies = {
            "last": keep_last,
            "daily": keep_daily,
            "weekly": keep_weekly,
            "monthly": keep_monthly,
            "yearly": keep_yearly,
        }
        if all(value is None for value in policies.values()):
            return self._result(
                OperationType.PRUNE, started, "No retention policy configured - refusing to prune. Nothing was deleted."
            )
        try:
            repo, job_root, _ = self._repository_root(destination)
            self._repository_marker(repo)
            root = job_root / "snapshots"
            rows = self.list_snapshots(destination)
            viable = self._viable_rows(rows, root, job_root.name, source_path)
            if rows and not viable:
                raise ValueError("No hash-valid snapshots are available; refusing to prune")
            remove = self._prune_candidates(viable, policies)
            candidates = [row["full_id"] for row in remove]
            if not dry_run:
                current_rows = self.list_snapshots(destination)
                current = self._prune_candidates(
                    self._viable_rows(current_rows, root, job_root.name, source_path), policies
                )
                if [row["full_id"] for row in current] != candidates:
                    raise ValueError("Snapshot set changed during retention preview; nothing was deleted")
                for row in current:
                    path = root / row["full_id"]
                    self._contained(path, root)
                    self._manifest(path, job_root.name)
                    try:
                        shutil.rmtree(path)
                    except OSError as error:
                        survivors = [item["full_id"] for item in current if (root / item["full_id"]).exists()]
                        raise OSError(f"Prune incomplete; snapshots still present: {', '.join(survivors)}") from error
            return self._result(
                OperationType.PRUNE,
                started,
                files_transferred=len(remove),
                metadata={"deleted_snapshot_ids": candidates, "dry_run": dry_run},
            )
        except (OSError, ValueError) as error:
            return self._result(OperationType.PRUNE, started, error)

    def _viable_rows(
        self, rows: list[dict[str, Any]], root: Path, job_name: str, source_path: str | None = None
    ) -> list[dict[str, Any]]:
        viable = []
        for row in rows:
            try:
                manifest = self._verify_snapshot(root / self._snapshot_id(row["full_id"]), job_name)
            except (OSError, ValueError):
                continue
            if source_path is not None and manifest.get("source_path") != source_path:
                continue
            viable.append(row)
        return viable

    @staticmethod
    def _prune_candidates(rows: list[dict[str, Any]], policies: dict[str, int | None]) -> list[dict[str, Any]]:
        """Keep the newest snapshot in each requested retention bucket."""
        retained: set[str] = set()
        if rows:
            # Never delete the sole viable snapshot, even for a zero-valued policy.
            retained.add(rows[0]["full_id"])
        keep_last = policies["last"]
        if keep_last is not None:
            retained.update(row["full_id"] for row in rows[: max(1, keep_last)])
        buckets = {
            "daily": lambda value: value.date().isoformat(),
            "weekly": lambda value: f"{value.isocalendar().year}-{value.isocalendar().week:02}",
            "monthly": lambda value: f"{value.year}-{value.month:02}",
            "yearly": lambda value: str(value.year),
        }
        for name, bucket in buckets.items():
            count = policies[name]
            if count is None:
                continue
            kept_buckets: set[str] = set()
            for row in rows:
                try:
                    timestamp = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
                except ValueError as error:
                    raise ValueError(f"Snapshot has an invalid timestamp: {row['full_id']}") from error
                key = bucket(timestamp)
                if len(kept_buckets) < count and key not in kept_buckets:
                    kept_buckets.add(key)
                    retained.add(row["full_id"])
        return [row for row in rows if row["full_id"] not in retained]
