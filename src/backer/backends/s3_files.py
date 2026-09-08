"""Unencrypted S3 snapshots, with a manifest published only after every file."""

from __future__ import annotations

import json
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from backer.backends.base import BackendResult, BackupDestination, BackupSource, OperationType
from backer.backends.files import FilesBackend
from backer.serverless.s3_sidecar import S3Sidecar


class S3FilesBackend(FilesBackend):
    @staticmethod
    def _safe_relative(value: str):
        if ":" in value or "\0" in value or "\\" in value:
            raise ValueError("Unsafe S3 file path")
        path = FilesBackend._safe_relative(value)
        if path.as_posix() != value:
            raise ValueError("Non-canonical S3 file path")
        return path

    def test_connection(self, destination: BackupDestination) -> tuple[bool, str]:
        status, _ = self.repository_probe(destination.path)
        return status == "present", "Repository is accessible" if status == "present" else getattr(
            self, "last_repository_error", "Repository is absent"
        )

    def _transport(self, location: str, *, job: bool = True) -> tuple[S3Sidecar, str]:
        settings = dict(self.config.get("s3") or {})
        parsed = urlsplit(location)
        prefix = str(settings.get("prefix") or "").strip("/")
        if prefix:
            self._safe_relative(prefix)
        if parsed.scheme != "s3" or parsed.netloc != settings.get("bucket") or parsed.query or parsed.fragment:
            raise ValueError("S3 destination does not match its configured bucket")
        expected = prefix + "/" if prefix else ""
        path = parsed.path.strip("/")
        job_name = ""
        if job:
            if not path.startswith(expected + "Agents/"):
                raise ValueError("Files snapshots require a destination beneath Agents/<job>")
            job_name = self._snapshot_id(path[len(expected + "Agents/") :])
        elif path != prefix:
            raise ValueError("S3 destination does not match its configured prefix")
        settings["prefix"] = prefix
        return S3Sidecar(settings, settings), job_name

    def _remote_marker(self, transport: S3Sidecar) -> bytes:
        data = transport.get(".backer/repository.json")
        if data is None:
            raise ValueError("Files repository marker is missing")
        marker = json.loads(data)
        if not isinstance(marker, dict) or marker.get("format") != "files" or marker.get("format_version") != 1:
            raise ValueError("Repository format marker is incompatible with files format v1")
        if not marker.get("repository_id") or (
            self.config.get("repository_id") and marker["repository_id"] != self.config["repository_id"]
        ):
            raise ValueError("Repository ID does not match the files repository marker")
        return data

    @contextmanager
    def _local(self, transport: S3Sidecar, job_name: str):
        # ponytail: check/prune stage all snapshots; verify sequentially when repositories outgrow scratch space.
        with TemporaryDirectory(prefix="backer-s3-files-") as temporary:
            root = Path(temporary) / "repository"
            marker = root / ".backer" / "repository.json"
            marker.parent.mkdir(parents=True)
            marker.write_bytes(self._remote_marker(transport))
            yield FilesBackend(self.config), BackupDestination(str(root / "Agents" / job_name))

    @contextmanager
    def _lock(self, transport: S3Sidecar, job_name: str):
        # No expiry: a crashed writer needs manual lock removal, never unsafe automatic takeover.
        key = f".backer/files-locks/{job_name}"
        try:
            transport.put_if_absent(key, uuid4().hex.encode())
        except Exception as error:
            raise RuntimeError(
                f"Cannot acquire S3 writer lock {transport._key(key)}. "
                "If a previous writer crashed, remove this object only after confirming no writer is running. "
                f"{error}"
            ) from error
        try:
            yield
        finally:
            transport.delete(key)

    def repository_probe(self, path: str) -> tuple[str, str | None]:
        try:
            transport, _ = self._transport(path, job=False)
            if transport.get(".backer/repository.json") is None:
                return "absent", None
            return "present", json.loads(self._remote_marker(transport))["repository_id"]
        except Exception as error:
            self.last_repository_error = str(error)
            return "unreachable", None

    def init_repo(self, destination: BackupDestination) -> BackendResult:
        started = datetime.now()
        try:
            transport, _ = self._transport(destination.path, job=False)
            if transport.list(""):
                raise ValueError("Files repository destination must be empty")
            transport.put_if_absent(
                ".backer/repository.json",
                json.dumps(
                    {
                        "repository_id": self.config.get("repository_id") or str(uuid4()),
                        "format": "files",
                        "format_version": 1,
                    },
                    sort_keys=True,
                ).encode(),
            )
            return self._result(OperationType.BACKUP, started)
        except Exception as error:
            return self._result(OperationType.BACKUP, started, error)

    def _manifests(
        self, transport: S3Sidecar, job_name: str, local: FilesBackend, destination: BackupDestination
    ) -> dict[str, bytes]:
        root = f"Agents/{job_name}/snapshots/"
        full_prefix = transport._key(root).rstrip("/") + "/"
        manifests = {}
        for key in transport.list(root):
            if not key.startswith(full_prefix):
                raise ValueError("S3 listing escaped the job snapshot prefix")
            relative = key[len(full_prefix) :]
            if relative.count("/") != 1 or not relative.endswith("/manifest.json"):
                continue
            snapshot_id = self._snapshot_id(relative[: -len("/manifest.json")])
            data = transport.get(root + snapshot_id + "/manifest.json")
            if data is None:
                raise ValueError("Snapshot disappeared during listing")
            snapshot = Path(destination.path) / "snapshots" / snapshot_id
            snapshot.mkdir(parents=True)
            (snapshot / "manifest.json").write_bytes(data)
            manifest = local._manifest(snapshot, job_name)
            for row in manifest["files"]:
                self._safe_relative(row["path"])
            manifests[snapshot_id] = data
        return manifests

    def _download(
        self, transport: S3Sidecar, job_name: str, local: FilesBackend, destination: BackupDestination, snapshot_id: str
    ) -> None:
        snapshot = Path(destination.path) / "snapshots" / self._snapshot_id(snapshot_id)
        manifest = local._manifest(snapshot, job_name)
        for row in manifest["files"]:
            if self._cancelled():
                raise InterruptedError("Operation cancelled")
            relative = self._safe_relative(row["path"])
            target = snapshot / "contents" / relative
            local._contained(target, snapshot / "contents")
            target.parent.mkdir(parents=True, exist_ok=True)
            transport.download(f"Agents/{job_name}/snapshots/{snapshot_id}/contents/{relative.as_posix()}", target)
        local._verify_snapshot(snapshot, job_name)

    def list_snapshots(self, destination: BackupDestination) -> list[dict[str, Any]]:
        transport, job_name = self._transport(destination.path)
        with self._local(transport, job_name) as (local, local_destination):
            self._manifests(transport, job_name, local, local_destination)
            return local.list_snapshots(local_destination)

    def backup(
        self,
        source: BackupSource,
        destination: BackupDestination,
        dry_run: bool = False,
        progress_callback: Any | None = None,
    ) -> BackendResult:
        started = datetime.now()
        try:
            transport, job_name = self._transport(destination.path)
            with self._local(transport, job_name) as (local, local_destination):
                with nullcontext() if dry_run else self._lock(transport, job_name):
                    self._manifests(transport, job_name, local, local_destination)
                    # Always create a new immutable object namespace; interrupted uploads cannot be reused.
                    local.config = {**self.config, "snapshot_id": str(uuid4())}
                    result = local.backup(source, local_destination, dry_run, progress_callback)
                    if not result.success or dry_run:
                        return result
                    snapshot_id = result.metadata["snapshot_id"]
                    snapshot = Path(local_destination.path) / "snapshots" / snapshot_id
                    manifest = local._verify_snapshot(snapshot, job_name)
                    for row in manifest["files"]:
                        self._safe_relative(row["path"])
                    root = f"Agents/{job_name}/snapshots/{snapshot_id}/"
                    for row in manifest["files"]:
                        if self._cancelled():
                            raise InterruptedError("Backup cancelled")
                        transport.upload_if_absent(
                            root + "contents/" + row["path"], snapshot / "contents" / row["path"]
                        )
                    if self._cancelled():
                        raise InterruptedError("Backup cancelled")
                    transport.put_if_absent(root + "manifest.json", (snapshot / "manifest.json").read_bytes())
                    return result
        except Exception as error:
            return self._result(OperationType.BACKUP, started, error)

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
        try:
            transport, job_name = self._transport(source.path)
            with self._local(transport, job_name) as (local, local_destination):
                self._manifests(transport, job_name, local, local_destination)
                rows = local.list_snapshots(local_destination)
                selected = snapshot or (rows[0]["full_id"] if rows else None)
                if not selected:
                    raise ValueError("No completed snapshots are available")
                self._download(transport, job_name, local, local_destination, selected)
                return local.restore(
                    local_destination,
                    destination,
                    selected,
                    dry_run,
                    progress_callback,
                    original_source_path,
                    include_path,
                )
        except Exception as error:
            return self._result(OperationType.RESTORE, started, error)

    def check(self, destination: BackupDestination) -> BackendResult:
        started = datetime.now()
        try:
            transport, job_name = self._transport(destination.path)
            with self._local(transport, job_name) as (local, local_destination):
                manifests = self._manifests(transport, job_name, local, local_destination)
                for snapshot_id in manifests:
                    self._download(transport, job_name, local, local_destination, snapshot_id)
                return local.check(local_destination)
        except Exception as error:
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
        try:
            policies = (keep_last, keep_daily, keep_weekly, keep_monthly, keep_yearly)
            if all(value is None for value in policies):
                raise ValueError("No retention policy configured - refusing to prune. Nothing was deleted.")
            transport, job_name = self._transport(destination.path)
            with self._local(transport, job_name) as (local, local_destination):
                with nullcontext() if dry_run else self._lock(transport, job_name):
                    manifests = self._manifests(transport, job_name, local, local_destination)
                    for snapshot_id in manifests:
                        self._download(transport, job_name, local, local_destination, snapshot_id)
                    result = local.prune(local_destination, *policies, dry_run=True, source_path=source_path)
                    if not result.success or dry_run:
                        return result
                    for snapshot_id, data in manifests.items():
                        if transport.get(f"Agents/{job_name}/snapshots/{snapshot_id}/manifest.json") != data:
                            raise ValueError("Snapshot set changed; refusing to prune")
                    for snapshot_id in result.metadata["deleted_snapshot_ids"]:
                        if self._cancelled():
                            raise InterruptedError("Retention cancelled")
                        manifest = json.loads(manifests[snapshot_id])
                        root = f"Agents/{job_name}/snapshots/{snapshot_id}/"
                        # Hide the snapshot first: interruption leaves unused objects, never a broken restore point.
                        transport.delete(root + "manifest.json")
                        for row in manifest["files"]:
                            transport.delete(root + "contents/" + self._safe_relative(row["path"]).as_posix())
                    result.metadata["dry_run"] = False
                    return result
        except Exception as error:
            return self._result(OperationType.PRUNE, started, error)
