"""Kopia backend implementation."""

import hashlib
import inspect
import json
import logging
import os
import re
import subprocess
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any

from backer.backends.base import (
    BackendBase,
    BackendResult,
    BackendType,
    BackupDestination,
    BackupSource,
    OperationType,
)
from backer.backends.s3 import kopia_s3_config
from backer.tools.manager import get_tool_manager

logger = logging.getLogger(__name__)


def _is_wrong_password_error(stderr: str) -> bool:
    """True if a kopia connect failure was a wrong passphrase, not an absent/unreachable repo.

    Observed against the pinned 0.23.1 binary: a wrong password against a
    real repository fails with "invalid repository password"; an absent
    repository fails with "repository not initialized in the provided
    storage"; an unreachable path/endpoint fails with "can't connect to
    storage". Only the password case is unambiguous.
    """
    return "invalid repository password" in stderr.lower()


def _is_not_initialized_error(stderr: str) -> bool:
    """True if a kopia connect failure means "no repository here" (vs. unreachable/wrong-password).

    Observed against the pinned 0.23.1 binary: "repository not initialized in
    the provided storage". This is the ONLY connect failure that ever
    justifies auto-creating a repository.
    """
    return "repository not initialized" in stderr.lower()


def _serialize_by_repo(path_from: str):
    """Decorator: serialize one method's kopia calls per-repository.

    `path_from` names the parameter holding the repository - either a
    BackupDestination/BackupSource (whose `.path` is used) or a plain path
    string. Mirrors server/app.py's `_serialized_kopia_operation`, which
    does the same for local repositories on the server side; many jobs
    commonly point at the same repository, and each connect/disconnect
    sequence must not interleave with another's.
    """

    def decorator(method):
        sig = inspect.signature(method)

        @wraps(method)
        def wrapped(self, *args, **kwargs):
            bound = sig.bind(self, *args, **kwargs)
            bound.apply_defaults()
            value = bound.arguments[path_from]
            path = value if isinstance(value, str) else value.path
            with self._repo_lock(path):
                return method(self, *args, **kwargs)

        return wrapped

    return decorator


def _normalize_source_path(p: str) -> str:
    """Normalize a source path for exact-match comparison.

    Deliberately narrow: only collapses the differences that are cosmetic,
    not a different location - trailing slashes, and (on Windows only)
    backslash-vs-forward-slash separators and case. Never falls back to
    matching on basename alone: two different directories that happen to
    share a name (e.g. C:\\Users\\alice\\Documents and D:\\Archive\\Documents)
    must never be treated as the same snapshot source.
    """
    normalized = p.rstrip("/\\")
    if os.name == "nt":
        normalized = normalized.replace("\\", "/").casefold()
    return normalized


class KopiaBackend(BackendBase):
    """Backend for Kopia deduplicated backups.

    Kopia provides:
    - Deduplication
    - Encryption
    - Snapshots
    - Multiple storage backends (filesystem, S3, Azure, GCS, etc.)
    - Compression
    - Content-addressable storage
    """

    backend_type = BackendType.KOPIA

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self._tool_manager = get_tool_manager()
        self._binary_path: Path | None = None
        self._env = os.environ.copy()

        password = self.config.get("repository_password")
        if password:
            self._env["KOPIA_PASSWORD"] = password
        self._has_repository_password = bool(password)

    def _get_binary(self, auto_install: bool = True) -> Path:
        """Get path to kopia binary, downloading if necessary."""
        if self._binary_path and self._binary_path.exists():
            return self._binary_path

        path = self._tool_manager.get_tool_path("kopia")
        if path:
            self._binary_path = path
            return path

        if auto_install:
            self._binary_path = self._tool_manager.download("kopia")
            return self._binary_path

        raise RuntimeError("kopia not installed. Run 'backer setup' to install tools.")

    def _repo_env(self, path: str) -> dict[str, str]:
        """Build the kopia environment for one repository.

        Kopia is connection-oriented: KOPIA_CONFIG_PATH names the file that
        records which repository is "current", and every later command
        (snapshot/policy/maintenance) operates on that connected repository
        with no repo argument. A single shared config (the previous default
        location) means two repositories - or two operations against
        different repositories - fight over which one is connected, and
        `repository disconnect` from one tears down the other mid-flight.
        Deriving a config path (and cache dir, which is also keyed off
        repository identity) from a hash of the repository path gives each
        repository its own isolated connection state.

        Two operations against the SAME repository path still share this one
        config file and can race on connect/disconnect - many jobs pointing
        at one repository is the normal deployment for a deduplicating
        backup tool, and the agent only dedupes concurrency by job name, so
        that race is the common case, not an edge case. `_repo_lock` (below)
        serializes each connect/use/disconnect sequence per repository, the
        same way ServerKopia._serialized_kopia_operation does for local
        repositories.
        """
        from backer.core.config import get_state_dir  # local import: avoids a circular import at module load time

        env = self._env.copy()
        key = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        repo_dir = get_state_dir() / "kopia-repos" / key
        env["KOPIA_CONFIG_PATH"] = str(repo_dir / "repository.config")
        env["KOPIA_CACHE_DIRECTORY"] = str(repo_dir / "cache")
        return env

    def _repo_lock_path(self, path: str) -> Path:
        """Lock file path for the repository at `path` - next to its per-repo config, not inside
        the repository itself, so this works for repositories on a network share too."""
        from backer.core.config import get_state_dir  # local import: avoids a circular import at module load time

        key = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        return get_state_dir() / "kopia-repos" / key / "repo.lock"

    def _repo_lock(self, path: str):
        """Serialize connect/use/disconnect sequences against one repository.

        Reuses backer.core.repo_metadata.file_lock (the same primitive
        ServerKopia._serialized_kopia_operation uses). file_lock defaults to
        mode "r+", which does not truncate - important here, since mode "w"
        truncates the file before locking it, which would let two callers
        both believe they'd truncated-then-locked an empty file. The lock
        file's contents are never read; only its existence is used to lock.
        """
        from backer.core.repo_metadata import file_lock  # local import: avoids a circular import at module load time

        return file_lock(self._repo_lock_path(path))

    def check_available(self) -> tuple[bool, str]:
        """Check if kopia is available, downloading if needed."""
        try:
            binary = self._get_binary(auto_install=False)
        except RuntimeError:
            try:
                binary = self._get_binary(auto_install=True)
            except Exception as e:
                return False, f"kopia not available and auto-install failed: {e}"

        try:
            result = subprocess.run(
                [str(binary), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return False, f"kopia version check failed: {result.stderr.strip()}"
            return True, result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError) as e:
            return False, str(e)

    def _get_repo_type(self, path: str) -> tuple[str, list[str]]:
        """Determine repository type and extra args from path.

        Returns:
            Tuple of (repo_type, extra_args)

        Raises:
            ValueError: If the path format is invalid (e.g., empty bucket name)
        """
        if path.startswith("s3://"):
            s3 = self.config.get("s3")
            if not isinstance(s3, dict):
                raise ValueError("S3 repository configuration is required")
            config = kopia_s3_config(s3)
            self._env.update(config["environment"])
            return "s3", config["options"]
        elif path.startswith("gs://"):
            # Google Cloud Storage
            remainder = path[5:]
            if not remainder or remainder.startswith("/"):
                raise ValueError(f"Invalid GCS path '{path}': bucket name is required")
            parts = remainder.split("/", 1)
            bucket = parts[0]
            prefix = parts[1] if len(parts) > 1 else ""
            return "gcs", ["--bucket", bucket, "--prefix", prefix]
        elif path.startswith("azure://"):
            # Azure Blob Storage
            remainder = path[8:]
            if not remainder or remainder.startswith("/"):
                raise ValueError(f"Invalid Azure path '{path}': container name is required")
            parts = remainder.split("/", 1)
            container = parts[0]
            prefix = parts[1] if len(parts) > 1 else ""
            return "azure", ["--container", container, "--prefix", prefix]
        elif path.startswith("sftp://"):
            # SFTP
            remainder = path[7:]
            if not remainder:
                raise ValueError(f"Invalid SFTP path '{path}': host/path is required")
            return "sftp", ["--path", remainder]
        else:
            # Local filesystem (default)
            if not path:
                raise ValueError("Repository path cannot be empty")
            return "filesystem", ["--path", path]

    @_serialize_by_repo("destination")
    def init_repo(self, destination: BackupDestination) -> BackendResult:
        """Initialize a new kopia repository."""
        started_at = datetime.now()

        if not self._has_repository_password:
            return BackendResult(
                success=False, operation=OperationType.BACKUP, started_at=started_at,
                finished_at=datetime.now(), errors=["Repository encryption password is required"], return_code=-1,
            )

        try:
            binary = self._get_binary()
            repo_type, repo_args = self._get_repo_type(destination.path)

            cmd = [str(binary), "repository", "create", repo_type]
            cmd.extend(repo_args)

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=self._repo_env(destination.path),
                timeout=120,
            )

            return BackendResult(
                success=result.returncode == 0,
                operation=OperationType.BACKUP,
                started_at=started_at,
                finished_at=datetime.now(),
                output=result.stdout + result.stderr,
                return_code=result.returncode,
                errors=[result.stderr] if result.returncode != 0 else [],
            )
        except (subprocess.TimeoutExpired, OSError, RuntimeError) as e:
            return BackendResult(
                success=False,
                operation=OperationType.BACKUP,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[str(e)],
                return_code=-1,
            )

    def _connect_repo(self, path: str) -> tuple[bool, str]:
        """Connect to an existing kopia repository."""
        if not self._has_repository_password:
            return False, "Repository encryption password is required"
        try:
            binary = self._get_binary()
            repo_type, repo_args = self._get_repo_type(path)

            cmd = [str(binary), "repository", "connect", repo_type]
            cmd.extend(repo_args)

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=self._repo_env(path),
                timeout=60,
            )

            if result.returncode == 0:
                return True, "Connected to repository"
            else:
                return False, result.stderr.strip()
        except Exception as e:
            return False, str(e)

    def _disconnect_repo(self, path: str) -> None:
        """Disconnect from the repository at path."""
        try:
            binary = self._get_binary()
            subprocess.run(
                [str(binary), "repository", "disconnect"],
                capture_output=True,
                text=True,
                env=self._repo_env(path),
                timeout=10,
            )
        except Exception:
            # Disconnect errors are non-fatal and expected when not connected
            pass

    def _auto_init_repo(self, path: str) -> tuple[bool, str]:
        """Create a repository for a backup that found none connected there.

        Only ever called from `backup()` after a connect failure that
        matched `_is_not_initialized_error` - never on a wrong password or
        an unreachable path. See the comment in `backup()` for the residual
        "unmounted share looks like an empty local directory" hazard this
        does not close.

        The one cheap guard applied here: for a filesystem-type destination
        (local path, or a mapped/mounted SMB or NFS path - anything that
        isn't an s3://, gs://, azure:// or sftp:// URL), refuse to create if
        the path already exists as a non-empty directory. Kopia itself
        would happily "create" a repository inside a directory that
        already holds unrelated files, so this is the difference between
        "nothing here yet" and "something here that isn't a repository or
        is a stale mount point" - it's a real signal, but it says nothing
        about the EMPTY case, which still passes straight through.
        """
        if not path.startswith(("s3://", "gs://", "azure://", "sftp://")):
            p = Path(path)
            try:
                if p.exists() and p.is_dir() and any(p.iterdir()):
                    return False, (
                        f"'{path}' already exists and is not empty - refusing to initialize a repository there"
                    )
            except OSError as e:
                return False, str(e)

        try:
            binary = self._get_binary()
            repo_type, repo_args = self._get_repo_type(path)
            cmd = [str(binary), "repository", "create", repo_type]
            cmd.extend(repo_args)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=self._repo_env(path),
                timeout=120,
            )
            return result.returncode == 0, result.stderr.strip()
        except (subprocess.TimeoutExpired, OSError, ValueError) as e:
            return False, str(e)

    @_serialize_by_repo("destination")
    def test_connection(self, destination: BackupDestination) -> tuple[bool, str]:
        """Test S3 access without creating a repository."""
        connected, message = self._connect_repo(destination.path)
        if connected:
            self._disconnect_repo(destination.path)
            return True, "Repository is accessible"
        if "repository does not exist" in message.lower() or "not initialized" in message.lower():
            return True, "S3 is accessible; repository will be initialized on first backup"
        return False, message

    @_serialize_by_repo("destination")
    def backup(
        self,
        source: BackupSource,
        destination: BackupDestination,
        dry_run: bool = False,
        progress_callback: Any | None = None,
    ) -> BackendResult:
        """Run kopia backup (snapshot create)."""
        started_at = datetime.now()

        try:
            binary = self._get_binary()
        except RuntimeError as e:
            return BackendResult(
                success=False,
                operation=OperationType.BACKUP,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[str(e)],
                return_code=-1,
            )

        # Connect to the repository. Repository creation at add time
        # (server/app.py's ServerKopia.ensure_repo) only covers repo_type ==
        # "local" - for SMB/NFS/S3 destinations nothing else ever creates
        # the repository, so the first backup to a genuinely new one must be
        # able to. We auto-create ONLY on kopia's "repository not
        # initialized in the provided storage" - never on a wrong password
        # or an unreachable/unmounted path, which must keep failing loudly.
        #
        # Residual hazard, and it is real: kopia reports that exact same
        # "not initialized" message whether the repository is genuinely
        # absent OR the destination resolved to an unmounted share that
        # looks, to kopia, like an empty local directory - kopia cannot
        # tell those apart, and neither can we from its error text alone.
        # The one cheap signal we do check (see _auto_init_repo): a
        # filesystem-type destination that already exists as a non-empty
        # directory is refused rather than initialized into, since that's
        # either unrelated data or a stale mount point - never something
        # safe to build a fresh repository inside. A destination that
        # resolves to an EMPTY directory (the classic "share never
        # mounted, fell through to a bare local folder" case) still looks
        # identical to "nothing here yet" and can still be auto-created
        # into by mistake. That gap is not closed here.
        connected, err = self._connect_repo(destination.path)
        auto_init_attempted = False
        if not connected and _is_not_initialized_error(err):
            # `repository create` also connects, so a successful auto-init leaves us
            # ready to proceed exactly as if _connect_repo had succeeded.
            auto_init_attempted = True
            connected, err = self._auto_init_repo(destination.path)

        if not connected:
            if _is_wrong_password_error(err):
                message = f"Wrong repository password for {destination.path}: {err}"
            elif auto_init_attempted:
                message = f"Repository at {destination.path} was not initialized and could not be auto-created: {err}"
            else:
                message = (
                    f"Failed to connect to repository at {destination.path}: {err}. "
                    "The repository must already be initialized (see init_repo) - "
                    "backup does not create one automatically."
                )
            return BackendResult(
                success=False,
                operation=OperationType.BACKUP,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[message],
                return_code=-1,
            )

        try:
            env = self._repo_env(destination.path)

            # Build snapshot command
            cmd = [str(binary), "snapshot", "create", "--json"]

            if dry_run:
                cmd.append("--dry-run")

            # Replace the source policy so changed or removed job excludes cannot leak into later runs.
            policy_cmd = [str(binary), "policy", "set", str(source.path), "--clear-ignore"]
            for exclude in source.excludes or []:
                policy_cmd.extend(["--add-ignore", exclude])
            policy = subprocess.run(
                policy_cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=self.config.get("timeout", 86400),
            )
            if policy.returncode != 0:
                return BackendResult(
                    success=False,
                    operation=OperationType.BACKUP,
                    started_at=started_at,
                    finished_at=datetime.now(),
                    errors=[line for line in policy.stderr.split("\n") if line.strip()],
                    return_code=policy.returncode,
                )

            # Add tags from config
            for tag in self.config.get("tags", []):
                cmd.extend(["--tags", tag])

            cmd.append(str(source.path))

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=self.config.get("timeout", 86400),
            )

            finished_at = datetime.now()

            # Parse JSON output for stats
            stats: dict[str, Any] = {}
            snapshot_id = None
            errors = []

            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    # Kopia outputs different JSON structures
                    if "rootEntry" in data:
                        root = data.get("rootEntry", {})
                        summary = root.get("summ", {})
                        stats = {
                            "total_size": summary.get("size", 0),
                            "total_files": summary.get("files", 0),
                            "total_dirs": summary.get("dirs", 0),
                        }
                    if "id" in data:
                        snapshot_id = data.get("id")
                    elif "snapshotID" in data:
                        snapshot_id = data.get("snapshotID")
                except json.JSONDecodeError:
                    pass

            if result.returncode != 0:
                errors = [line for line in result.stderr.split("\n") if line.strip()]

            return BackendResult(
                success=result.returncode == 0,
                operation=OperationType.BACKUP,
                started_at=started_at,
                finished_at=finished_at,
                bytes_transferred=stats.get("total_size", 0),
                files_transferred=stats.get("total_files", 0),
                errors=errors,
                output=result.stdout + result.stderr,
                return_code=result.returncode,
                metadata={"snapshot_id": snapshot_id, **stats},
            )

        except subprocess.TimeoutExpired:
            return BackendResult(
                success=False,
                operation=OperationType.BACKUP,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=["Backup operation timed out"],
                return_code=-1,
            )
        except OSError as e:
            return BackendResult(
                success=False,
                operation=OperationType.BACKUP,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[f"Failed to execute kopia: {e}"],
                return_code=-1,
            )
        finally:
            self._disconnect_repo(destination.path)

    def _find_latest_snapshot_for_source(self, repo_path: str, source_path: str) -> str | None:
        """Find the latest snapshot ID for a given source path.

        Args:
            repo_path: The repository (destination) path to look up snapshots in
            source_path: The original source path that was backed up

        Returns:
            The snapshot ID if found, None otherwise
        """
        try:
            binary = self._get_binary()

            # List snapshots filtered by source path
            # kopia snapshot list --json returns snapshots with source info
            result = subprocess.run(
                [str(binary), "snapshot", "list", "--json", "--all"],
                capture_output=True,
                text=True,
                env=self._repo_env(repo_path),
                timeout=60,
            )

            if result.returncode != 0 or not result.stdout.strip():
                print(f"[KOPIA] Failed to list snapshots: {result.stderr}")
                return None

            snapshots = json.loads(result.stdout)

            # Filter snapshots by exact source path (normalized narrowly -
            # trailing slashes, and Windows separator/case differences).
            # Never fall back to matching on basename alone: two different
            # directories that merely share a name must never be confused,
            # or a restore could silently return someone else's data.
            normalized_target = _normalize_source_path(source_path)
            matching_snapshots = []
            for snap in snapshots:
                snap_source = snap.get("source", {}).get("path", "")
                if snap_source and _normalize_source_path(snap_source) == normalized_target:
                    matching_snapshots.append(snap)

            if not matching_snapshots:
                print(f"[KOPIA] No snapshots found for source path: {source_path}")
                print("[KOPIA] Available sources:")
                for snap in snapshots[:5]:  # Show first 5
                    print(f"  - {snap.get('source', {}).get('path', 'unknown')}")
                return None

            # Sort by start time (most recent first)
            matching_snapshots.sort(
                key=lambda x: x.get("startTime", ""),
                reverse=True
            )

            latest = matching_snapshots[0]
            snapshot_id = latest.get("id")
            print(f"[KOPIA] Found {len(matching_snapshots)} matching snapshot(s), using latest: {snapshot_id}")
            return snapshot_id

        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as e:
            print(f"[KOPIA] Error finding snapshot: {e}")
            return None

    @_serialize_by_repo("source")
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
        """Restore from kopia snapshot."""
        started_at = datetime.now()

        if dry_run:
            return BackendResult(
                success=False,
                operation=OperationType.RESTORE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=["Kopia restore dry runs are not supported"],
                return_code=-1,
            )

        try:
            binary = self._get_binary()
        except RuntimeError as e:
            return BackendResult(
                success=False,
                operation=OperationType.RESTORE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[str(e)],
                return_code=-1,
            )

        # Connect to repository first
        connected, err = self._connect_repo(source.path)
        if not connected:
            return BackendResult(
                success=False,
                operation=OperationType.RESTORE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[f"Failed to connect to repository: {err}"],
                return_code=-1,
            )

        try:
            # Determine the snapshot identifier to restore
            # If user provided a specific snapshot ID, use it directly
            # Only look up "latest" if no snapshot was specified
            snapshot_id = snapshot

            if not snapshot_id or snapshot_id == "latest":
                if original_source_path:
                    # Use the original source path to find the latest snapshot
                    print(f"[KOPIA] Looking up latest snapshot for source: {original_source_path}")
                    latest_id = self._find_latest_snapshot_for_source(source.path, original_source_path)
                    if latest_id:
                        snapshot_id = latest_id
                        print(f"[KOPIA] Found latest snapshot: {snapshot_id}")
                    else:
                        # Fall back to using the source path directly
                        snapshot_id = f"{original_source_path}@latest"
                        print(f"[KOPIA] Using source path qualifier: {snapshot_id}")
                else:
                    snapshot_id = "latest"
            else:
                print(f"[KOPIA] Using user-specified snapshot: {snapshot_id}")

            if include_path:
                snapshot_id = f"{snapshot_id.rstrip('/')}/{include_path.lstrip('/')}"

            # Kopia restore syntax: kopia snapshot restore <snapshot-id> <target-path>
            # Add flags to overwrite existing files and restore all content
            cmd = [
                str(binary), "snapshot", "restore",
                "--overwrite-files",
                "--overwrite-directories",
                "--overwrite-symlinks",
                snapshot_id,
                str(destination),
            ]

            print(f"[KOPIA] Running restore command: {' '.join(cmd)}")

            # Kopia doesn't have a direct --dry-run for restore
            # We could use --skip-existing or similar

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=self._repo_env(source.path),
                timeout=self.config.get("timeout", 86400),
            )

            errors = []
            if result.returncode != 0:
                errors = [line for line in result.stderr.split("\n") if line.strip()]

            return BackendResult(
                success=result.returncode == 0,
                operation=OperationType.RESTORE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=errors,
                output=result.stdout + result.stderr,
                return_code=result.returncode,
                metadata={"snapshot": snapshot_id},
            )

        except subprocess.TimeoutExpired:
            return BackendResult(
                success=False,
                operation=OperationType.RESTORE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=["Restore operation timed out"],
                return_code=-1,
            )
        except OSError as e:
            return BackendResult(
                success=False,
                operation=OperationType.RESTORE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[f"Failed to execute kopia: {e}"],
                return_code=-1,
            )
        finally:
            self._disconnect_repo(source.path)

    @_serialize_by_repo("destination")
    def list_snapshots(self, destination: BackupDestination) -> list[dict[str, Any]]:
        """List available kopia snapshots."""
        try:
            binary = self._get_binary()

            # Connect to repository first
            connected, _ = self._connect_repo(destination.path)
            if not connected:
                return []

            try:
                result = subprocess.run(
                    [str(binary), "snapshot", "list", "--json", "--all"],
                    capture_output=True,
                    text=True,
                    env=self._repo_env(destination.path),
                    timeout=60,
                )

                if result.returncode == 0 and result.stdout.strip():
                    snapshots = json.loads(result.stdout)
                    return [
                        {
                            "id": snap.get("id", "unknown")[:12],
                            "full_id": snap.get("id"),
                            "timestamp": snap.get("startTime"),
                            "hostname": snap.get("hostname"),
                            "username": snap.get("username"),
                            "paths": [snap.get("source", {}).get("path", "")],
                            "tags": snap.get("tags", []),
                            "size": snap.get("stats", {}).get("totalSize", 0),
                        }
                        for snap in snapshots
                    ]
            finally:
                self._disconnect_repo(destination.path)

        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, RuntimeError) as e:
            logger.warning(f"[KOPIA] Failed to list snapshots: {e}")

        return []

    def _resolve_source_target(self, repo_path: str, source_path: str) -> str | None:
        """Build the kopia 'user@host:path' target for a given source path.

        Looks up the actual snapshot source recorded by kopia (via `snapshot
        list --json`) rather than guessing the local username/hostname, so
        the target we hand to `policy set` / `snapshot expire` is exactly
        what kopia already recorded. Returns None if no matching source is
        found - callers must refuse rather than fall back to a repo-wide
        target.
        """
        try:
            binary = self._get_binary()
            result = subprocess.run(
                [str(binary), "snapshot", "list", "--json", "--all"],
                capture_output=True,
                text=True,
                env=self._repo_env(repo_path),
                timeout=60,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None

            snapshots = json.loads(result.stdout)
            normalized = _normalize_source_path(source_path)
            for snap in snapshots:
                source = snap.get("source", {})
                snap_path = source.get("path", "")
                if snap_path and _normalize_source_path(snap_path) == normalized:
                    host = source.get("host")
                    user = source.get("userName")
                    if not host or not user:
                        return None
                    return f"{user}@{host}:{snap_path}"
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            return None

        return None

    @_serialize_by_repo("destination")
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
        """Prune old snapshots using kopia policy and maintenance.

        dry_run=True never writes the retention policy - kopia has no
        preview mode for "policy set"; it persists immediately and is
        applied at the next snapshot regardless of --delete. So a dry run
        here reports what the CURRENTLY PERSISTED policy would expire, not
        what keep_last/keep_daily/... passed to this call would expire. Run
        with dry_run=False to actually apply and evaluate the proposed
        policy.
        """
        started_at = datetime.now()

        # FAIL CLOSED: never expire snapshots under a retention policy nobody
        # configured. Without this, "policy set --global" with no keep flags
        # is a no-op and "snapshot expire --delete" deletes under whatever
        # policy happens to already be in effect (kopia defaults, or a policy
        # left behind by another job sharing this repository).
        if not any((keep_last, keep_daily, keep_weekly, keep_monthly, keep_yearly)):
            return BackendResult(
                success=False,
                operation=OperationType.PRUNE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[
                    "No retention policy configured (keep_last/keep_daily/keep_weekly/"
                    "keep_monthly/keep_yearly all unset) - refusing to prune. Nothing "
                    "was deleted."
                ],
                return_code=-1,
            )

        try:
            binary = self._get_binary()
        except RuntimeError as e:
            return BackendResult(
                success=False,
                operation=OperationType.PRUNE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[str(e)],
                return_code=-1,
            )

        # Connect to repository first
        connected, err = self._connect_repo(destination.path)
        if not connected:
            return BackendResult(
                success=False,
                operation=OperationType.PRUNE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[f"Failed to connect to repository: {err}"],
                return_code=-1,
            )

        try:
            env = self._repo_env(destination.path)
            target: str | None = None
            if source_path is not None:
                target = self._resolve_source_target(destination.path, source_path)
                if target is None:
                    return BackendResult(
                        success=False,
                        operation=OperationType.PRUNE,
                        started_at=started_at,
                        finished_at=datetime.now(),
                        errors=[
                            f"Could not resolve a kopia snapshot source matching "
                            f"'{source_path}' - refusing to prune rather than risk "
                            f"applying the policy repository-wide."
                        ],
                        return_code=-1,
                    )

            # Set retention policy - scoped to the resolved source when given,
            # otherwise the repository-wide global policy (a caller that
            # deliberately prunes the whole repository is legitimate).
            #
            # NEVER run this on a dry run: "policy set" is not a preview, it
            # PERSISTS the retention policy to the repository. Kopia applies
            # whatever policy is persisted at the *next* "snapshot create" -
            # not just on an explicit "snapshot expire --delete" - so writing
            # it here would mean a dry run has already armed real deletion at
            # the next ordinary backup, with nothing linking the loss back to
            # the preview. A dry run must only ever read state, never write it.
            if not dry_run:
                policy_cmd = [str(binary), "policy", "set"]
                policy_cmd.append(target if target is not None else "--global")

                if keep_last:
                    policy_cmd.extend(["--keep-latest", str(keep_last)])
                if keep_daily:
                    policy_cmd.extend(["--keep-daily", str(keep_daily)])
                if keep_weekly:
                    policy_cmd.extend(["--keep-weekly", str(keep_weekly)])
                if keep_monthly:
                    policy_cmd.extend(["--keep-monthly", str(keep_monthly)])
                if keep_yearly:
                    policy_cmd.extend(["--keep-annual", str(keep_yearly)])

                policy_result = subprocess.run(
                    policy_cmd,
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=60,
                )

                if policy_result.returncode != 0:
                    return BackendResult(
                        success=False,
                        operation=OperationType.PRUNE,
                        started_at=started_at,
                        finished_at=datetime.now(),
                        errors=[policy_result.stderr],
                        output=policy_result.stdout + policy_result.stderr,
                        return_code=policy_result.returncode,
                    )

            # Run snapshot expire to apply retention. "snapshot expire" without
            # --delete only reports what would be removed, so that IS the dry
            # run; there is no --dry-run flag (kopia rejects it as unknown).
            #
            # NOTE: because the policy above is never written on a dry run,
            # this reports what the CURRENTLY PERSISTED policy would expire,
            # not what the keep_* args passed to this call would expire. A
            # dry run answers "what would happen if a backup ran right now",
            # not "what would happen if this proposed policy were applied".
            expire_cmd = [str(binary), "snapshot", "expire"]
            if target is not None:
                expire_cmd.append(target)
            else:
                expire_cmd.append("--all")
            if not dry_run:
                expire_cmd.append("--delete")

            expire_result = subprocess.run(
                expire_cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=self.config.get("timeout", 3600),
            )

            # Also run maintenance to clean up deleted data
            if not dry_run and expire_result.returncode == 0:
                maint_result = subprocess.run(
                    [str(binary), "maintenance", "run", "--full"],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=self.config.get("timeout", 3600),
                )
                output = expire_result.stdout + expire_result.stderr + maint_result.stdout + maint_result.stderr
                return_code = maint_result.returncode
            else:
                output = expire_result.stdout + expire_result.stderr
                return_code = expire_result.returncode

            errors = []
            if return_code != 0:
                errors = [line for line in output.split("\n") if line.strip() and "error" in line.lower()]

            if dry_run:
                output = (
                    "[dry-run] Proposed policy was NOT saved. This shows what the "
                    "CURRENTLY PERSISTED retention policy would expire, not the "
                    "policy passed to this call.\n" + output
                )

            return BackendResult(
                success=return_code == 0,
                operation=OperationType.PRUNE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=errors,
                output=output,
                return_code=return_code,
            )

        except (subprocess.TimeoutExpired, OSError) as e:
            return BackendResult(
                success=False,
                operation=OperationType.PRUNE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[str(e)],
                return_code=-1,
            )
        finally:
            self._disconnect_repo(destination.path)

    @_serialize_by_repo("destination")
    def check(self, destination: BackupDestination) -> BackendResult:
        """Check repository integrity using kopia maintenance."""
        started_at = datetime.now()

        try:
            binary = self._get_binary()

            # Connect to repository first
            connected, err = self._connect_repo(destination.path)
            if not connected:
                return BackendResult(
                    success=False,
                    operation=OperationType.CHECK,
                    started_at=started_at,
                    finished_at=datetime.now(),
                    errors=[f"Failed to connect to repository: {err}"],
                    return_code=-1,
                )

            try:
                # "repository validate-client" is not a real kopia subcommand.
                # "snapshot verify" is the actual integrity check; by default it
                # only verifies metadata (--verify-files-percent=0), reading no
                # file content, which is fast. Reading actual content is much
                # slower, so it stays opt-in via config.
                verify_percent = self.config.get("verify_files_percent", 0)
                result = subprocess.run(
                    [
                        str(binary),
                        "snapshot",
                        "verify",
                        f"--verify-files-percent={verify_percent}",
                    ],
                    capture_output=True,
                    text=True,
                    env=self._repo_env(destination.path),
                    timeout=self.config.get("timeout", 3600),
                )

                return BackendResult(
                    success=result.returncode == 0,
                    operation=OperationType.CHECK,
                    started_at=started_at,
                    finished_at=datetime.now(),
                    output=result.stdout + result.stderr,
                    return_code=result.returncode,
                )
            finally:
                self._disconnect_repo(destination.path)

        except (subprocess.TimeoutExpired, OSError, RuntimeError) as e:
            return BackendResult(
                success=False,
                operation=OperationType.CHECK,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[str(e)],
                return_code=-1,
            )

    # kopia's `ls` output has no --json mode; one line per entry, e.g.:
    #   drwxrwxrwx            6 2026-08-31 23:48:18 AEST k090c70a25a6eac07a41461bbfe109552  sub/
    #   -rw-rw-rw-            6 2026-08-31 23:48:18 AEST f218bb89b4c096463f45e07b2ef3a5ef   a.txt
    # mode, size, date, time, timezone, object id, name (dirs keep a trailing "/").
    _LS_LINE_RE = re.compile(
        r"^(?P<mode>\S+)\s+(?P<size>\d+)\s+(?P<date>\d{4}-\d{2}-\d{2})\s+"
        r"(?P<time>\d{2}:\d{2}:\d{2})\s+(?P<tz>\S+)\s+(?P<oid>\S+)\s+(?P<name>.+)$"
    )

    @_serialize_by_repo("destination")
    def get_snapshot_files(
        self,
        destination: BackupDestination,
        snapshot_id: str,
        path: str = "",
    ) -> list[dict[str, Any]]:
        """List files in a snapshot (for browsing before restore).

        `snapshot list` takes a source path and lists snapshot history, not
        a snapshot's contents, and it has no --path flag. Listing what's
        inside a snapshot is `kopia ls <object-path>`, where a full snapshot
        id is itself a valid directory object at the snapshot root, so
        `<snapshot_id>/<path>` addresses a subdirectory within it directly.

        Takes the FULL snapshot id. `list_snapshots` truncates its "id" to 12
        characters and keeps the full value in "full_id", so a short id is
        resolved back through it rather than handed to kopia, which rejects
        it with "is not a directory object".
        """
        try:
            binary = self._get_binary()

            connected, _ = self._connect_repo(destination.path)
            if not connected:
                return []

            try:
                full_id = snapshot_id
                if len(snapshot_id) < 32:
                    match = next(
                        (
                            snap.get("full_id")
                            for snap in self.list_snapshots(destination)
                            if snapshot_id in (snap.get("id"), snap.get("full_id"))
                        ),
                        None,
                    )
                    if not match:
                        logger.warning(f"[KOPIA] Unknown snapshot id: {snapshot_id}")
                        return []
                    full_id = match

                object_path = f"{full_id}/{path}" if path else full_id
                result = subprocess.run(
                    [str(binary), "ls", "--long", "--show-object-id", object_path],
                    capture_output=True,
                    text=True,
                    env=self._repo_env(destination.path),
                    timeout=60,
                )

                if result.returncode != 0:
                    logger.warning(f"[KOPIA] Failed to get snapshot files: {result.stderr.strip()}")
                    return []

                entries = []
                for line in result.stdout.splitlines():
                    m = self._LS_LINE_RE.match(line)
                    if not m:
                        continue
                    name = m.group("name")
                    is_dir = m.group("mode").startswith("d") or name.endswith("/")
                    entries.append(
                        {
                            "name": name[:-1] if is_dir else name,
                            "type": "dir" if is_dir else "file",
                            "size": int(m.group("size")),
                            "mtime": f"{m.group('date')} {m.group('time')} {m.group('tz')}",
                            "object_id": m.group("oid"),
                        }
                    )
                return entries
            finally:
                self._disconnect_repo(destination.path)

        except (subprocess.TimeoutExpired, OSError, RuntimeError) as e:
            logger.warning(f"[KOPIA] Failed to get snapshot files: {e}")

        return []
