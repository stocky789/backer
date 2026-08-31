"""FastAPI application for the Backer server."""

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from backer import __version__
from backer.core.repo_metadata import file_lock
from backer.server import timezone as tz
from backer.server.auth import (
    enrollment_code_expired,
    generate_agent_token,
    generate_proxy_capability,
    hash_enrollment_code,
    verify_agent_token,
    verify_expired_proxy_capability,
    verify_proxy_capability,
)
from backer.server.models import (
    BackupResult,
    Client,
    ClientHeartbeat,
    ClientRegisterRequest,
    ClientRegisterResponse,
    ClientStatus,
    JobCreate,
    JobResponse,
    JobRunRequest,
    JobRunResponse,
)
from backer.server.repository_paths import get_job_subfolder as _get_job_subfolder
from backer.server.scheduler import BackupScheduler
from backer.server.storage import Storage
from backer.server.tasks import Task, get_task_manager
from backer.server.web.routes import router as web_router

logger = logging.getLogger(__name__)

_SECRET_KEYS = {
    "password",
    "kopia_password",
    "repository_password",
    "storage_password",
    "client_secret",
    "proxy_capability",
    "access_key_id",
    "secret_access_key",
}


def _redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("***" if key.lower() in _SECRET_KEYS else _redact_secrets(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    return value


def _serialized_kopia_operation(method):
    """Serialize each connect/use/disconnect sequence for one repository."""

    @wraps(method)
    def locked(self, *args, **kwargs):
        with file_lock(self.repo_path.resolve() / ".backer-kopia.lock"):
            return method(self, *args, **kwargs)

    return locked


class ServerKopia:
    """Server-side kopia helper for proxy backup storage.

    Handles kopia repository operations on the server for LOCAL repositories
    that receive backup data via the proxy protocol.
    """

    def __init__(self, repo_path: str, password: str):
        """Initialize with repository path and password.

        Args:
            repo_path: Path to the local storage directory
            password: Repository password for encryption
        """
        self.repo_path = Path(repo_path)
        self.kopia_repo_path = self.repo_path / ".kopia-repo"
        self.password = password
        self._binary_path: Path | None = None
        self._env = os.environ.copy()
        self._env["KOPIA_PASSWORD"] = password

        # Use a unique config per repository to avoid conflicts
        config_dir = self.repo_path / ".kopia-config"
        config_dir.mkdir(parents=True, exist_ok=True)
        self._env["KOPIA_CONFIG_PATH"] = str(config_dir / "repository.config")

    def _get_binary(self) -> Path:
        """Get path to kopia binary."""
        if self._binary_path and self._binary_path.exists():
            return self._binary_path

        from backer.tools.manager import get_tool_manager

        manager = get_tool_manager()
        path = manager.get_tool_path("kopia")
        if path:
            self._binary_path = path
            return path

        # Try to download
        try:
            self._binary_path = manager.download("kopia")
            return self._binary_path
        except Exception as e:
            raise RuntimeError(f"kopia not available: {e}")

    def _run_cmd(
        self,
        args: list[str],
        timeout: int = 300,
        check: bool = True,
    ) -> tuple[int, str, str]:
        """Run a kopia command.

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        import subprocess

        binary = self._get_binary()
        cmd = [str(binary)] + args

        logger.debug(f"[SERVER KOPIA] Running: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=self._env,
                timeout=timeout,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Command timed out: {' '.join(args)}")

    @_serialized_kopia_operation
    def ensure_repo(self) -> bool:
        """Ensure kopia repository exists, creating if necessary.

        Returns:
            True if repository is ready
        """
        # Try to connect first
        rc, stdout, stderr = self._run_cmd(
            [
                "repository",
                "connect",
                "filesystem",
                "--path",
                str(self.kopia_repo_path),
            ],
            check=False,
        )

        if rc == 0:
            logger.info(f"[SERVER KOPIA] Connected to repository at {self.kopia_repo_path}")
            self._disconnect()
            return True

        # Repository doesn't exist - initialize it
        logger.info(f"[SERVER KOPIA] Initializing repository at {self.kopia_repo_path}")
        self.kopia_repo_path.mkdir(parents=True, exist_ok=True)

        rc, stdout, stderr = self._run_cmd(
            [
                "repository",
                "create",
                "filesystem",
                "--path",
                str(self.kopia_repo_path),
            ],
            check=False,
        )

        if rc != 0:
            logger.error(f"[SERVER KOPIA] Failed to initialize repository: {stderr}")
            return False

        logger.info("[SERVER KOPIA] Repository initialized successfully")
        self._disconnect()
        return True

    def _connect(self) -> bool:
        """Connect to the repository."""
        rc, _, stderr = self._run_cmd(
            [
                "repository",
                "connect",
                "filesystem",
                "--path",
                str(self.kopia_repo_path),
            ],
            check=False,
        )

        if rc != 0:
            logger.error(f"[SERVER KOPIA] Failed to connect: {stderr}")
            return False
        return True

    def _disconnect(self) -> None:
        """Disconnect from the repository."""
        try:
            self._run_cmd(["repository", "disconnect"], timeout=10, check=False)
        except Exception:
            pass

    @_serialized_kopia_operation
    def snapshot_create(
        self,
        source_dir: str | Path,
        job_name: str,
        source_path: str = "",
    ) -> dict[str, Any]:
        """Create a snapshot of the source directory.

        Args:
            source_dir: Directory to snapshot
            job_name: Job name for tagging
            source_path: Original source path on agent (for reference)

        Returns:
            Dict with success status and snapshot info
        """
        if not self._connect():
            return {"success": False, "error": "Failed to connect to repository"}

        try:
            # Build snapshot command with tags
            args = [
                "snapshot",
                "create",
                "--json",
                "--tags",
                f"job:{job_name}",
            ]
            if source_path:
                args.extend(["--tags", f"source:{source_path}"])

            args.append(str(source_dir))

            rc, stdout, stderr = self._run_cmd(args, timeout=3600)

            if rc != 0:
                logger.error(f"[SERVER KOPIA] Snapshot failed: {stderr}")
                return {"success": False, "error": stderr}

            # Parse snapshot info from JSON output
            snapshot_id = None
            for line in stdout.strip().split("\n"):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if "id" in data:
                        snapshot_id = data.get("id")
                    elif "snapshotID" in data:
                        snapshot_id = data.get("snapshotID")
                except json.JSONDecodeError:
                    pass

            logger.info(f"[SERVER KOPIA] Created snapshot: {snapshot_id}")
            return {
                "success": True,
                "snapshot_id": snapshot_id,
                "message": f"Snapshot created: {snapshot_id}",
            }

        finally:
            self._disconnect()

    def _snapshot_list(self, job_name: str | None = None) -> list[dict[str, Any]]:
        """List snapshots, optionally filtered by job name.

        Args:
            job_name: Optional job name to filter by

        Returns:
            List of snapshot info dicts
        """
        if not self._connect():
            return []

        try:
            args = ["snapshot", "list", "--json", "--all"]

            rc, stdout, stderr = self._run_cmd(args, timeout=60)

            if rc != 0 or not stdout.strip():
                logger.warning(f"[SERVER KOPIA] Failed to list snapshots: {stderr}")
                return []

            try:
                snapshots = json.loads(stdout)
            except json.JSONDecodeError:
                logger.error("[SERVER KOPIA] Failed to parse snapshot list JSON")
                return []

            logger.info(f"[SERVER KOPIA] Found {len(snapshots)} total snapshots")

            # Log first snapshot structure for debugging
            if snapshots:
                first = snapshots[0]
                logger.info(f"[SERVER KOPIA] Sample snapshot keys: {list(first.keys())}")
                logger.info(f"[SERVER KOPIA] Sample tags: {first.get('tags')}")

            # Filter by job tag if specified
            # Note: kopia stores tags with "tag:" prefix (e.g., "tag:job" not "job")
            result = []
            for snap in snapshots:
                tags = snap.get("tags", {})
                snap_job = tags.get("tag:job", "")

                logger.info(f"[SERVER KOPIA] Snapshot {snap.get('id', '')[:12]}: tags={tags}")

                if job_name and snap_job != job_name:
                    continue

                result.append(
                    {
                        "id": snap.get("id", "")[:12],
                        "full_id": snap.get("id"),
                        "timestamp": snap.get("startTime"),
                        "hostname": snap.get("hostname"),
                        "source": snap.get("source", {}).get("path", ""),
                        "job": snap_job,
                        "size": snap.get("stats", {}).get("totalSize", 0),
                        "files": snap.get("stats", {}).get("totalFiles", 0),
                    }
                )

            # Sort by timestamp descending (newest first)
            result.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

            if job_name and not result:
                logger.warning(f"[SERVER KOPIA] No snapshots for job '{job_name}' (total: {len(snapshots)})")

            return result

        finally:
            self._disconnect()

    @_serialized_kopia_operation
    def snapshot_list(self, job_name: str | None = None) -> list[dict[str, Any]]:
        return self._snapshot_list(job_name)

    @_serialized_kopia_operation
    def snapshot_restore(
        self,
        snapshot_id: str,
        dest_dir: str | Path,
    ) -> dict[str, Any]:
        """Restore a snapshot to a directory.

        Args:
            snapshot_id: Snapshot ID to restore (or "latest")
            dest_dir: Directory to restore to

        Returns:
            Dict with success status
        """
        if not self._connect():
            return {"success": False, "error": "Failed to connect to repository"}

        try:
            dest_path = Path(dest_dir)
            dest_path.mkdir(parents=True, exist_ok=True)

            args = [
                "snapshot",
                "restore",
                "--overwrite-files",
                "--overwrite-directories",
                "--overwrite-symlinks",
                snapshot_id,
                str(dest_path),
            ]

            rc, stdout, stderr = self._run_cmd(args, timeout=3600)

            if rc != 0:
                logger.error(f"[SERVER KOPIA] Restore failed: {stderr}")
                return {"success": False, "error": stderr}

            logger.info(f"[SERVER KOPIA] Restored snapshot {snapshot_id} to {dest_path}")
            return {
                "success": True,
                "message": f"Restored snapshot {snapshot_id}",
            }

        finally:
            self._disconnect()

    @_serialized_kopia_operation
    def find_latest_snapshot(self, job_name: str) -> str | None:
        """Find the latest snapshot ID for a job.

        Args:
            job_name: Job name to search for

        Returns:
            Snapshot ID or None
        """
        snapshots = self._snapshot_list(job_name)
        if snapshots:
            return snapshots[0].get("full_id")
        return None

    @_serialized_kopia_operation
    def maintenance(self, args: list[str]) -> dict[str, Any]:
        """Run a non-secret Kopia maintenance command against this repo."""
        if not self._connect():
            return {"success": False, "error": "Failed to connect to repository"}
        try:
            rc, stdout, stderr = self._run_cmd(args, timeout=3600, check=False)
            return {"success": rc == 0, "output": stdout, "error": stderr or None}
        finally:
            self._disconnect()


# Characters not allowed in job names (prevent path traversal and shell injection)
_UNSAFE_NAME_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]|\.\.|\.\/')


def validate_name(name: str, field: str = "name") -> None:
    """Validate a name field to prevent path traversal and injection.

    Raises HTTPException if invalid.
    """
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail=f"{field} cannot be empty")
    if len(name) > 255:
        raise HTTPException(status_code=400, detail=f"{field} too long (max 255 chars)")
    if _UNSAFE_NAME_PATTERN.search(name):
        raise HTTPException(
            status_code=400,
            detail=f"{field} contains invalid characters (no path separators, quotes, or special chars)",
        )


def safe_tar_extract(tar_path: str | Path, dest_path: str | Path) -> list[str]:
    """Safely extract a tar archive with path traversal protection.

    Validates each member before extraction to prevent:
    - Absolute paths that could write outside dest_path
    - Relative paths with .. that escape dest_path
    - Symbolic links pointing outside dest_path

    Args:
        tar_path: Path to the tar archive
        dest_path: Destination directory for extraction

    Returns:
        List of extracted member names

    Raises:
        ValueError: If archive contains unsafe paths
    """
    import tarfile

    dest = Path(dest_path).resolve()
    members = []

    with tarfile.open(str(tar_path), "r:gz") as tar:
        for member in tar.getmembers():
            # Check for absolute paths (both Unix and Windows style)
            if member.name.startswith("/") or member.name.startswith("\\"):
                raise ValueError(f"Tar member has absolute path: {member.name}")

            # Check for path traversal (both Unix and Windows separators)
            if ".." in member.name.split("/") or ".." in member.name.split("\\"):
                raise ValueError(f"Tar member has path traversal: {member.name}")

            # Resolve the final path and ensure it's within dest
            # Use is_relative_to() for proper cross-platform comparison
            # (handles case-insensitivity on Windows)
            member_path = (dest / member.name).resolve()
            try:
                member_path.relative_to(dest)
            except ValueError:
                raise ValueError(f"Tar member escapes destination: {member.name}")

            # Check symlinks don't point outside
            if member.issym() or member.islnk():
                link_target = Path(member.linkname)
                if link_target.is_absolute():
                    raise ValueError(f"Tar symlink has absolute target: {member.name}")
                resolved_link = (member_path.parent / link_target).resolve()
                try:
                    resolved_link.relative_to(dest)
                except ValueError:
                    raise ValueError(f"Tar symlink escapes destination: {member.name}")

            members.append(member.name)

        # All members validated, now extract
        tar.extractall(path=str(dest))

    return members


# Global storage instance (initialized in create_app)
_storage: Storage | None = None
_scheduler: BackupScheduler | None = None

security = HTTPBasic(auto_error=False)


def get_storage() -> Storage:
    """Get the storage instance."""
    if _storage is None:
        raise RuntimeError("Storage not initialized")
    return _storage


def _repository_password_or_error(password: str | None) -> str:
    if not password:
        raise HTTPException(status_code=400, detail="Repository encryption password is required")
    return password


# kopia policy flag for each retention key, keyed to match RetentionConfig
_RETENTION_KOPIA_FLAGS = {
    "keep_last": "--keep-latest",
    "keep_daily": "--keep-daily",
    "keep_weekly": "--keep-weekly",
    "keep_monthly": "--keep-monthly",
    "keep_yearly": "--keep-annual",
}


def _validate_retention_policy(body: dict[str, Any]) -> dict[str, int]:
    """Extract and validate keep_* retention values from a request body.

    Raises ValueError if a supplied value is present but not a positive int.
    Returns only the keys that were actually supplied - never fills in a
    default for a missing or invalid one, since that would silently apply
    a policy nobody configured.
    """
    policy: dict[str, int] = {}
    for key in _RETENTION_KOPIA_FLAGS:
        if key not in body or body[key] is None:
            continue
        value = body[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{key} must be a positive integer")
        policy[key] = value
    return policy


def _build_backup_command_payload(
    job: dict[str, Any],
    job_name: str,
    run_id: str,
    dry_run: bool = False,
    storage: Storage | None = None,
    client_id: str | None = None,
) -> dict[str, Any]:
    """Build an engine-free, repository-driven backup command payload."""
    storage = storage or _storage
    if storage is None:
        raise RuntimeError("Storage not initialized")

    # Check if the target client is an Android device
    # Android agents cannot mount SMB/NFS shares, so they must use proxy backend
    is_android_client = False
    if client_id:
        client = storage.get_client(client_id)
        if client:
            os_info = getattr(client, "os_info", "") or ""
            is_android_client = os_info.lower().startswith("android")
            if is_android_client:
                logger.debug(f"[BACKUP] Detected Android client: {client_id} (os_info={os_info})")

    job_subfolder = _get_job_subfolder(job_name)
    repository_id = job.get("repository_id")
    if not repository_id:
        raise ValueError("A repository is required for backups")
    repo = storage.get_repository(repository_id)
    if not repo:
        raise ValueError("Repository not found")
    repo_type = repo.get("repo_type", "")
    if is_android_client and repo_type != "local":
        raise ValueError("Android agents support local repositories only")
    repository_password = storage.get_repository_password(repository_id)
    if not repository_password:
        raise ValueError("Repository encryption password is required")

    payload: dict[str, Any] = {
        "job_name": job_name,
        "run_id": run_id,
        "source_path": job.get("source_path"),
        "excludes": job.get("excludes", []),
        "repository_options": {"repository_password": repository_password},
        "dry_run": dry_run,
    }
    if repo_type == "smb":
        payload.update(
            {
                "destination_path": f"//{repo.get('server')}/{repo.get('share')}/Agents/{job_subfolder}",
                "smb_server": repo.get("server"),
                "smb_share": repo.get("share"),
                "smb_username": repo.get("username"),
                "smb_domain": repo.get("domain"),
            }
        )
        if storage_password := storage.get_storage_password(repository_id):
            payload["smb_password"] = storage_password
    elif repo_type == "nfs":
        payload.update(
            {
                "destination_path": f"{repo.get('server')}:{repo.get('share')}/Agents/{job_subfolder}",
                "nfs_server": repo.get("server"),
                "nfs_export": repo.get("share"),
            }
        )
    elif repo_type == "local":
        public_url = storage.get_setting("public_url", "http://localhost:8420")
        scheme = "proxys" if public_url.startswith("https://") else "proxy"
        host = public_url.removeprefix("https://").removeprefix("http://")
        payload["destination_path"] = f"{scheme}://{host}/repo/{repository_id}/Agents/{job_subfolder}"
        payload["repository_options"]["proxy_capability"] = generate_proxy_capability(
            client_id=client_id or "",
            repo_id=repository_id,
            job_name=job_name,
            run_id=run_id,
            subfolder=f"Agents/{job_subfolder}",
            operation="backup",
        )
    elif repo_type == "s3":
        from backer.backends.s3 import kopia_s3_config

        s3 = {
            **repo.get("config", {}).get("s3", {}),
            **(storage.get_repository_provider_credentials(repository_id) or {}),
        }
        payload["destination_path"] = kopia_s3_config(s3)["repository"]
        payload["repository_options"]["s3"] = s3
    else:
        raise ValueError(f"Unsupported repository type: {repo_type}")

    return payload


def _validate_job_config(config: dict[str, Any], storage: Storage) -> None:
    """Reject unsupported repository types before persistence."""
    repo_type = None
    if repo_id := config.get("repository_id"):
        repo = storage.get_repository(repo_id)
        if not repo:
            raise HTTPException(status_code=400, detail="Repository not found")
        repo_type = repo.get("repo_type")
    if repo_type and repo_type not in {"smb", "nfs", "local", "s3"}:
        raise HTTPException(status_code=400, detail=f"Unsupported repository type: {repo_type}")
    if client_id := config.get("client_id"):
        client = storage.get_client(client_id)
        if client and (client.os_info or "").lower().startswith("android"):
            if repo_type != "local":
                raise HTTPException(status_code=400, detail="Android agents support local repositories only")


def _restore_redacted_secrets(value: Any, existing: Any) -> Any:
    """Keep secrets hidden by GET unchanged when its response is PUT back."""
    if isinstance(value, list) and isinstance(existing, list):
        return [
            _restore_redacted_secrets(item, existing[index] if index < len(existing) else None)
            for index, item in enumerate(value)
        ]
    if not isinstance(value, dict) or not isinstance(existing, dict):
        return value
    return {
        key: (
            existing[key]
            if key.lower() in _SECRET_KEYS and item == "***" and key in existing
            else _restore_redacted_secrets(item, existing.get(key))
        )
        for key, item in value.items()
    }


def _refresh_proxy_capabilities(commands: list[dict[str, Any]], client_id: str) -> list[dict[str, Any]]:
    """Mint short-lived proxy credentials when a queued command is delivered."""
    for command in commands:
        payload = command.get("payload", {})
        operation = command.get("command_type")
        if not str(payload.get("destination_path" if operation == "backup" else "source_path", "")).lower().startswith(
            ("proxy://", "proxys://")
        ) or operation not in {"backup", "restore"}:
            continue
        proxy_path = payload.get("destination_path" if operation == "backup" else "source_path", "")
        match = re.search(r"/repo/([^/]+)/(.+)$", proxy_path)
        if not match:
            continue
        repo_id, subfolder = match.groups()
        payload.setdefault("repository_options", {})["proxy_capability"] = generate_proxy_capability(
            client_id=client_id,
            repo_id=repo_id,
            job_name=payload.get("job_name", ""),
            run_id=payload.get("run_id", ""),
            subfolder=subfolder,
            operation=operation,
        )
    return commands


def _pending_proxy_command_authorizes(
    storage: Storage,
    client_id: str,
    claims: dict[str, Any],
    operation: str,
) -> bool:
    """An expired capability is usable only while its exact command is queued."""
    command_operation = claims.get("operation")
    if claims.get("sub") != client_id or (
        command_operation != operation and not (operation == "check" and command_operation in {"backup", "restore"})
    ):
        return False
    for command in storage.get_pending_commands(client_id):
        payload = command["payload"]
        if command["command_type"] != command_operation or not str(
            payload.get("destination_path" if command_operation == "backup" else "source_path", "")
        ).lower().startswith(("proxy://", "proxys://")):
            continue
        path = payload.get("destination_path" if command_operation == "backup" else "source_path", "")
        match = re.search(r"/repo/([^/]+)/(.+)$", path)
        if match and (
            match.group(1),
            payload.get("job_name"),
            payload.get("run_id"),
            match.group(2),
            command_operation,
        ) == (
            claims.get("repo"),
            claims.get("job"),
            claims.get("run"),
            claims.get("subfolder"),
            claims.get("operation"),
        ):
            return True
    return False


def trigger_job_internal(job_name: str) -> None:
    """Internal function to trigger a job (used by scheduler).

    This bypasses HTTP and directly queues the backup command.
    """
    if _storage is None:
        logger.error("Storage not initialized")
        return

    job = _storage.get_job(job_name)
    if not job:
        logger.error(f"Job not found: {job_name}")
        return

    try:
        _validate_job_config(job, _storage)
    except HTTPException as exc:
        logger.error(f"Job {job_name} cannot run: {exc.detail}")
        return

    client_id = job.get("client_id")
    if not client_id:
        logger.error(f"Job {job_name} has no assigned agent")
        return

    client = _storage.get_client(client_id)
    if not client:
        logger.error(f"Agent {client_id} not found for job {job_name}")
        return

    now = tz.get_now()
    run_id = now.strftime("%Y%m%d_%H%M%S_%f")
    started_at = now

    # Save the run record as "pending"
    _storage.save_job_run(
        run_id=run_id,
        job_name=job_name,
        status="pending",
        started_at=started_at,
        client_id=client_id,
    )

    # Start progress tracking
    _storage.start_job_progress(
        run_id=run_id,
        job_name=job_name,
        client_id=client_id,
    )

    # Build and queue the backup command with repository credentials
    command_payload = _build_backup_command_payload(
        job=job,
        job_name=job_name,
        run_id=run_id,
        dry_run=False,
        client_id=client_id,
    )

    _storage.queue_command(
        client_id=client_id,
        command_type="backup",
        payload=command_payload,
    )

    logger.info(f"Scheduled job {job_name} queued for agent {client_id} (run_id: {run_id})")


def trigger_hypervisor_job_internal(job_id: str) -> None:
    """Internal function to trigger a hypervisor backup job (used by scheduler).

    This runs the backup synchronously in a background thread.
    For Proxmox: Backups are stored to the configured Backer repository by auto-configuring
    it as Proxmox storage (like Veeam does).
    For Unraid: Backups are performed via SSH to the Unraid server.
    """
    if _storage is None:
        logger.error("Storage not initialized")
        return

    job = _storage.get_hypervisor_job(job_id)
    if not job:
        logger.error(f"Hypervisor job not found: {job_id}")
        return

    hypervisor = _storage.get_hypervisor(job["hypervisor_id"])
    if not hypervisor:
        logger.error(f"Hypervisor not found for job: {job_id}")
        return

    hypervisor_type = hypervisor.get("hypervisor_type", "proxmox")

    if hypervisor_type == "unraid":
        _trigger_unraid_backup_job(job_id, job, hypervisor)
    elif hypervisor_type == "proxmox":
        _trigger_proxmox_backup_job(job_id, job, hypervisor)
    elif hypervisor_type == "hyperv":
        _trigger_hyperv_backup_job(job_id, job, hypervisor)
    elif hypervisor_type == "hyperv-cluster":
        _trigger_hyperv_cluster_backup_job(job_id, job, hypervisor)
    else:
        logger.error(f"Unsupported hypervisor type: {hypervisor_type}")


def _trigger_unraid_backup_job(job_id: str, job: dict, hypervisor: dict) -> None:
    """Trigger an Unraid backup job."""
    from backer.hypervisors.unraid import UnraidAPI, UnraidBackupManager

    # Get repository for backup destination
    repository_id = job.get("repository_id")
    if not repository_id:
        logger.error(f"No repository configured for job: {job_id}")
        return

    repository = _storage.get_repository(repository_id)
    if not repository:
        logger.error(f"Repository not found: {repository_id}")
        return

    # Validate repository type (must be SMB or NFS)
    repo_type = repository.get("repo_type", "").lower()
    if repo_type not in ("smb", "nfs"):
        logger.error(
            f"Repository type '{repo_type}' is not supported for Unraid backups. Use an SMB or NFS repository."
        )
        return

    # Get credentials
    token_secret = _storage.get_hypervisor_token_secret(hypervisor["id"])
    hv_password = _storage.get_hypervisor_password(hypervisor["id"])

    # Unraid uses API key authentication (stored as token_secret)
    api_key = token_secret or hv_password
    if not api_key:
        logger.error("No API key configured for Unraid hypervisor")
        return

    run_id = tz.get_now().strftime("%Y%m%d_%H%M%S_%f")
    mount_point = None
    backup_manager = None

    try:
        port = hypervisor.get("port", 443)
        api = UnraidAPI(
            host=hypervisor["host"],
            api_key=api_key,
            port=port,
            use_https=port in (443, 8443),
            verify_ssl=hypervisor.get("verify_ssl", False),
        )

        # Get SSH credentials
        ssh_user = hypervisor.get("ssh_user", "root")
        ssh_port = hypervisor.get("ssh_port", 22)
        ssh_key_path = hypervisor.get("ssh_key_path")
        ssh_password = hv_password if hypervisor.get("ssh_use_api_password", True) else None

        backup_manager = UnraidBackupManager(
            api=api,
            ssh_host=hypervisor["host"],
            ssh_user=ssh_user,
            ssh_port=ssh_port,
            ssh_key_path=ssh_key_path,
            ssh_password=ssh_password,
        )

        # Build backup path on the repository
        # The repository could be:
        # 1. An Unraid share (server = unraid host, share = local share name)
        # 2. An external SMB/NFS share that needs to be mounted on Unraid
        repo_server = repository.get("server", "")
        repo_share = repository.get("share", "")
        repo_path = repository.get("path", "")

        repo_config = repository.get("config") or {}

        def hosts_match(host_a: str | None, host_b: str | None) -> bool:
            if not host_a or not host_b:
                return False
            ha = host_a.strip().lower()
            hb = host_b.strip().lower()
            if not ha or not hb:
                return False
            if ha == hb:
                return True
            try:
                return socket.gethostbyname(ha) == socket.gethostbyname(hb)
            except Exception:
                return False

        # Check if the repository is an Unraid local share
        # (explicit flag or host matches the Unraid server itself)
        is_local_share = bool(repo_config.get("unraid_local_share"))
        if not is_local_share:
            if not repo_server:
                is_local_share = True
            else:
                normalized = repo_server.strip().lower()
                is_local_share = hosts_match(repo_server, hypervisor["host"]) or normalized in (
                    "localhost",
                    "127.0.0.1",
                    "tower",
                    "tower.local",
                )

        mount_point = None
        if is_local_share:
            # Local Unraid share - path is /mnt/user/{share}/{path}
            if repo_path:
                backup_base_path = f"/mnt/user/{repo_share}/{repo_path}/Hypervisors/{hypervisor['name']}"
            else:
                backup_base_path = f"/mnt/user/{repo_share}/Hypervisors/{hypervisor['name']}"
        else:
            # External SMB/NFS share - need to mount on Unraid
            mount_point = f"/mnt/backer_repo_{repository['id'][:8]}"

            # Create mount point directory
            rc, _, stderr = backup_manager._run_ssh_command(f"mkdir -p '{mount_point}'")
            if rc != 0:
                logger.error(f"Failed to create mount point: {stderr}")
                return

            # Mount the share based on type
            credentials_file = None

            if repo_type == "smb":
                repo_password = _storage.get_storage_password(repository_id)
                repo_username = repository.get("username", "guest")
                repo_domain = repository.get("domain", "")

                # Build a credential file on the Unraid host to avoid exposing secrets via process args
                credentials_file = f"/tmp/backer_smb_{repository_id[:8]}"
                cred_lines = [
                    f"username={repo_username or ''}",
                    f"password={repo_password or ''}",
                ]
                if repo_domain:
                    cred_lines.append(f"domain={repo_domain}")

                cred_content = "\n".join(cred_lines) + "\n"
                rc, _, stderr = backup_manager._run_ssh_command(
                    f"cat <<'EOF' > '{credentials_file}'\n{cred_content}EOF"
                )
                if rc != 0:
                    logger.error(f"Failed to create SMB credentials file: {stderr}")
                    return
                backup_manager._run_ssh_command(f"chmod 600 '{credentials_file}'")

                mount_opts = f"credentials={credentials_file}"
                mount_cmd = f"mount -t cifs '//{repo_server}/{repo_share}' '{mount_point}' -o {mount_opts}"

            elif repo_type == "nfs":
                mount_cmd = f"mount -t nfs '{repo_server}:{repo_share}' '{mount_point}'"
            else:
                logger.error(f"Unsupported repository type for Unraid: {repo_type}")
                return

            rc, _, stderr = backup_manager._run_ssh_command(mount_cmd, timeout=60)
            if rc != 0:
                logger.error(f"Failed to mount repository on Unraid: {stderr}")
                # Clean up mount point
                backup_manager._run_ssh_command(f"rmdir '{mount_point}'")
                if credentials_file:
                    backup_manager._run_ssh_command(f"rm -f '{credentials_file}'")
                return

            logger.info(f"Mounted {repo_type.upper()} share at {mount_point}")
            if credentials_file:
                backup_manager._run_ssh_command(f"rm -f '{credentials_file}'")

            if repo_path:
                backup_base_path = f"{mount_point}/{repo_path}/Hypervisors/{hypervisor['name']}"
            else:
                backup_base_path = f"{mount_point}/Hypervisors/{hypervisor['name']}"

        # Get guest IDs from job
        guest_ids = job.get("guest_ids", [])

        if not guest_ids:
            # Backup all guests
            all_guests = backup_manager.list_all_guests()
            guest_ids = [g["vmid"] for g in all_guests]

        # Build guest info map
        all_guests = backup_manager.list_all_guests()
        guest_map = {g["vmid"]: g for g in all_guests}

        logger.info(
            f"Starting Unraid backup job '{job['name']}' to repository '{repository['name']}' ({len(guest_ids)} items)"
        )

        # Start job progress tracking for activity panel
        _storage.start_job_progress(
            run_id=run_id,
            job_name=job["name"],
        )
        _storage.update_job_progress(
            run_id=run_id,
            status="running",
            message=f"Starting backup of {len(guest_ids)} item(s)",
            total_files=len(guest_ids),
            files_processed=0,
        )

        # Collect results for metadata writing
        backup_results = []

        for idx, guest_id in enumerate(guest_ids):
            guest = guest_map.get(guest_id)
            if not guest:
                # Try to find by name if not found by ID
                guest = next((g for g in all_guests if g["name"] == guest_id), None)

            guest_name = guest["name"] if guest else str(guest_id)
            guest_type = guest.get("guest_type", "vm") if guest else "vm"

            # Update job progress for activity panel
            progress_pct = int((idx / len(guest_ids)) * 100)
            _storage.update_job_progress(
                run_id=run_id,
                status="running",
                progress_percent=progress_pct,
                message=f"Backing up {guest_type}: {guest_name}",
                current_file=guest_name,
                files_processed=idx,
            )

            # Save run as running
            _storage.save_hypervisor_run(
                run_id=run_id,
                job_id=job_id,
                job_name=job["name"],
                hypervisor_id=hypervisor["id"],
                guest_id=str(guest_id),
                guest_name=guest_name,
                guest_type=guest_type,
                status="running",
            )

            if guest_type == "share" and not api.supports_shares:
                warning = "Unraid API does not expose shares; skip share backup"
                logger.warning(warning)
                _storage.save_hypervisor_run(
                    run_id=run_id,
                    job_id=job_id,
                    job_name=job["name"],
                    hypervisor_id=hypervisor["id"],
                    guest_id=str(guest_id),
                    guest_name=guest_name,
                    guest_type=guest_type,
                    status="failed",
                    finished_at=tz.get_now(),
                    errors=[warning],
                )
                continue

            try:
                # Determine backup path based on guest type
                type_folder = {
                    "vm": "vms",
                    "docker": "containers",
                    "share": "shares",
                    "flash": "flash",
                }.get(guest_type, "other")
                backup_path = f"{backup_base_path}/{type_folder}"

                # Run the backup
                result = backup_manager.run_backup(
                    guest_type=guest_type,
                    guest_id=guest_name,  # Use name for Unraid operations
                    backup_path=backup_path,
                    options=job.get("backup_options", {}),
                )

                backup_size = result.get("bytes_transferred", 0)
                backup_filename = result.get("backup_file") or result.get("backup_dir")

                # Update run record
                _storage.save_hypervisor_run(
                    run_id=run_id,
                    job_id=job_id,
                    job_name=job["name"],
                    hypervisor_id=hypervisor["id"],
                    guest_id=str(guest_id),
                    guest_name=guest_name,
                    guest_type=guest_type,
                    status="success" if result.get("success") else "failed",
                    finished_at=tz.get_now(),
                    duration_seconds=result.get("duration_seconds"),
                    backup_size=backup_size,
                    backup_filename=backup_filename,
                    errors=result.get("errors"),
                )

                # Collect result for metadata
                backup_results.append(
                    {
                        "vmid": guest_id,
                        "guest_name": guest_name,
                        "guest_type": guest_type,
                        "success": result.get("success", False),
                        "backup_filename": backup_filename,
                        "backup_size": backup_size,
                        "duration_seconds": result.get("duration_seconds"),
                        "started_at": tz.get_now().isoformat(),
                        "finished_at": tz.get_now().isoformat(),
                        "errors": result.get("errors"),
                    }
                )

                if result.get("success"):
                    logger.info(
                        f"Backup succeeded for {guest_type} '{guest_name}' ({backup_size / 1024 / 1024:.1f} MB)"
                    )
                else:
                    logger.warning(f"Backup failed for {guest_type} '{guest_name}': {result.get('errors')}")

            except Exception as e:
                logger.exception(f"Backup failed for {guest_type} '{guest_name}': {e}")
                _storage.save_hypervisor_run(
                    run_id=run_id,
                    job_id=job_id,
                    job_name=job["name"],
                    hypervisor_id=hypervisor["id"],
                    guest_id=str(guest_id),
                    guest_name=guest_name,
                    guest_type=guest_type,
                    status="failed",
                    finished_at=tz.get_now(),
                    errors=[str(e)],
                )
                # Collect failed result for metadata
                backup_results.append(
                    {
                        "vmid": guest_id,
                        "guest_name": guest_name,
                        "guest_type": guest_type,
                        "success": False,
                        "errors": [str(e)],
                    }
                )

        # Cleanup: unmount external share if we mounted it
        if mount_point:
            logger.info(f"Unmounting {mount_point}")
            rc, _, stderr = backup_manager._run_ssh_command(f"umount '{mount_point}'", timeout=60)
            if rc != 0:
                logger.warning(f"Failed to unmount {mount_point}: {stderr}")
            else:
                # Remove mount point directory
                backup_manager._run_ssh_command(f"rmdir '{mount_point}'")

        # Finish job progress tracking
        success_count = sum(1 for r in backup_results if r.get("success"))
        fail_count = len(backup_results) - success_count
        final_status = "completed" if fail_count == 0 else "failed" if success_count == 0 else "completed"
        _storage.update_job_progress(
            run_id=run_id,
            status=final_status,
            progress_percent=100,
            message=f"Completed: {success_count} succeeded, {fail_count} failed",
            files_processed=len(guest_ids),
        )
        _storage.finish_job_progress(run_id, final_status)

        logger.info(f"Unraid backup job '{job.get('name')}' completed")

        # Write metadata to repository
        try:
            # Get repository password for metadata writing
            repo_password = _storage.get_storage_password(repository_id)
            repo_with_password = {**repository, "password": repo_password}

            _write_backup_metadata_to_repo(
                repository=repo_with_password,
                hypervisor=hypervisor,
                job=job,
                job_id=job_id,
                run_id=run_id,
                results=backup_results,
                guest_map=guest_map,
            )
        except Exception as meta_err:
            # Don't fail backup if metadata write fails
            logger.warning(f"Failed to write Unraid backup metadata: {meta_err}")

    except Exception as e:
        logger.exception(f"Unraid backup job {job_id} failed: {e}")
        # Mark job progress as failed
        try:
            _storage.update_job_progress(
                run_id=run_id,
                status="failed",
                message=f"Job failed: {e}",
            )
            _storage.finish_job_progress(run_id, "failed")
        except Exception:
            pass  # Best effort
        # Cleanup on failure: try to unmount if we mounted
        if mount_point and backup_manager:
            try:
                backup_manager._run_ssh_command(f"umount '{mount_point}'", timeout=60)
                backup_manager._run_ssh_command(f"rmdir '{mount_point}'")
            except Exception:
                pass  # Best effort cleanup


def _trigger_proxmox_backup_job(job_id: str, job: dict, hypervisor: dict) -> None:
    """Trigger a Proxmox backup job."""
    from backer.hypervisors.proxmox import (
        ProxmoxAPI,
        ProxmoxAPIError,
        ProxmoxAuthMethod,
        ProxmoxBackupManager,
        ProxmoxBackupMode,
        ProxmoxCompression,
    )

    # Get repository for backup destination
    repository_id = job.get("repository_id")
    if not repository_id:
        logger.error(f"No repository configured for job: {job_id}")
        return

    repository = _storage.get_repository(repository_id)
    if not repository:
        logger.error(f"Repository not found: {repository_id}")
        return

    # Validate repository type (must be SMB or NFS for Proxmox storage)
    repo_type = repository.get("repo_type", "").lower()
    if repo_type not in ("smb", "nfs"):
        logger.error(
            f"Repository type '{repo_type}' is not supported for hypervisor backups. "
            "Use an SMB or NFS repository so Proxmox can write directly to it."
        )
        return

    # Get credentials
    token_secret = _storage.get_hypervisor_token_secret(hypervisor["id"])
    hv_password = _storage.get_hypervisor_password(hypervisor["id"])

    auth_method = ProxmoxAuthMethod.TOKEN if hypervisor["auth_method"] == "token" else ProxmoxAuthMethod.PASSWORD

    run_id = tz.get_now().strftime("%Y%m%d_%H%M%S_%f")
    proxmox_storage_id = None  # Track for cleanup

    try:
        api = ProxmoxAPI(
            host=hypervisor["host"],
            port=hypervisor.get("port", 8006),
            auth_method=auth_method,
            username=hypervisor.get("username"),
            token_id=hypervisor.get("token_id"),
            token_secret=token_secret,
            password=hv_password,
            verify_ssl=hypervisor.get("verify_ssl", False),
        )

        # Authenticate if using password-based auth
        if auth_method == ProxmoxAuthMethod.PASSWORD:
            api.authenticate()

        # Get repository password for SMB storage
        repo_password = None
        if repo_type == "smb" and repository.get("has_password"):
            repo_password = _storage.get_storage_password(repository_id)

        # Create repository dict with password for ensure_backer_storage
        repo_with_password = {**repository, "password": repo_password}

        # Get SSH credentials for mount point cleanup
        ssh_user = job.get("ssh_user") or hypervisor.get("ssh_user", "root")
        ssh_port = job.get("ssh_port") or hypervisor.get("ssh_port", 22)
        ssh_key_path = hypervisor.get("ssh_key_path")

        # Get SSH password if configured to use API password for SSH
        ssh_password = None
        if hypervisor.get("ssh_use_api_password", True):
            ssh_password = _storage.get_hypervisor_password(hypervisor["id"])

        # Acquire Proxmox storage for this repository (with reference counting)
        # Backups go to: {repo_path}/Hypervisors/{hypervisor_name}/dump/
        try:
            proxmox_storage_id = api.acquire_backer_storage(
                repo_with_password,
                hypervisor_name=hypervisor["name"],
                ssh_user=ssh_user,
                ssh_port=ssh_port,
                ssh_key=ssh_key_path,
                ssh_password=ssh_password,
            )
        except ProxmoxAPIError as e:
            logger.error(f"Failed to configure Proxmox storage for repository: {e}")
            return

        backup_manager = ProxmoxBackupManager(api)

        # Get backup options
        mode_map = {
            "snapshot": ProxmoxBackupMode.SNAPSHOT,
            "stop": ProxmoxBackupMode.STOP,
            "suspend": ProxmoxBackupMode.SUSPEND,
        }
        mode = mode_map.get(job.get("backup_mode", "snapshot"), ProxmoxBackupMode.SNAPSHOT)

        compress_map = {
            "zstd": ProxmoxCompression.ZSTD,
            "gzip": ProxmoxCompression.GZIP,
            "lzo": ProxmoxCompression.LZO,
            "none": ProxmoxCompression.NONE,
        }
        compress = compress_map.get(job.get("compression", "zstd"), ProxmoxCompression.ZSTD)

        # Get guest IDs from job
        guest_ids = job.get("guest_ids", [])

        if not guest_ids:
            # Backup all guests
            guests = api.list_guests()
            guest_ids = [g.vmid for g in guests]

        # Build guest name map
        all_guests = api.list_guests()
        guest_map = {g.vmid: g for g in all_guests}

        logger.info(
            f"Starting hypervisor backup job '{job['name']}' to repository "
            f"'{repository['name']}' (Proxmox storage: {proxmox_storage_id})"
        )

        # Collect results for metadata writing
        backup_results = []

        # Start job progress tracking for activity panel
        _storage.start_job_progress(
            run_id=run_id,
            job_name=job["name"],
        )
        _storage.update_job_progress(
            run_id=run_id,
            status="running",
            message=f"Starting backup of {len(guest_ids)} guest(s)",
            total_files=len(guest_ids),
            files_processed=0,
        )

        for idx, vmid in enumerate(guest_ids):
            guest = guest_map.get(vmid)
            guest_name = guest.name if guest else f"VM {vmid}"
            guest_type = guest.guest_type.value if guest else "qemu"

            # Update job progress for activity panel
            progress_pct = int((idx / len(guest_ids)) * 100)
            _storage.update_job_progress(
                run_id=run_id,
                status="running",
                progress_percent=progress_pct,
                message=f"Backing up {guest_type}: {guest_name} (VMID {vmid})",
                current_file=guest_name,
                files_processed=idx,
            )

            # Save run as running
            _storage.save_hypervisor_run(
                run_id=run_id,
                job_id=job_id,
                job_name=job["name"],
                hypervisor_id=hypervisor["id"],
                guest_id=vmid,
                guest_name=guest_name,
                guest_type=guest_type,
                status="running",
            )

            try:
                # Backup directly to Proxmox storage (which points to Backer repo)
                result = backup_manager.backup_to_storage(
                    vmid=vmid,
                    storage=proxmox_storage_id,
                    mode=mode,
                    compress=compress,
                    retention=job.get("retention"),
                    timeout=7200,
                )

                backup_size = result.get("backup_size", 0)
                backup_filename = result.get("backup_filename")

                # Update run record
                _storage.save_hypervisor_run(
                    run_id=run_id,
                    job_id=job_id,
                    job_name=job["name"],
                    hypervisor_id=hypervisor["id"],
                    guest_id=vmid,
                    guest_name=guest_name,
                    guest_type=guest_type,
                    status="success" if result.get("success") else "failed",
                    upid=result.get("upid"),
                    finished_at=(
                        datetime.fromisoformat(result["finished_at"]) if result.get("finished_at") else tz.get_now()
                    ),
                    duration_seconds=result.get("duration_seconds"),
                    backup_size=backup_size,
                    backup_filename=backup_filename,
                    exit_status=result.get("exit_status"),
                    errors=result.get("errors"),
                )

                # Collect result for metadata
                backup_results.append(
                    {
                        "vmid": vmid,
                        "guest_name": guest_name,
                        "guest_type": guest_type,
                        "success": result.get("success", False),
                        "backup_filename": backup_filename,
                        "backup_size": backup_size,
                        "duration_seconds": result.get("duration_seconds"),
                        "started_at": tz.get_now().isoformat(),
                        "finished_at": (result.get("finished_at") or tz.get_now().isoformat()),
                        "errors": result.get("errors"),
                    }
                )

                if result.get("success"):
                    logger.info(
                        f"Backup succeeded for VMID {vmid} -> {proxmox_storage_id} ({backup_size / 1024 / 1024:.1f} MB)"
                    )
                else:
                    logger.warning(f"Backup failed for VMID {vmid}: {result.get('errors')}")

            except Exception as e:
                logger.exception(f"Backup failed for VMID {vmid}: {e}")
                _storage.save_hypervisor_run(
                    run_id=run_id,
                    job_id=job_id,
                    job_name=job["name"],
                    hypervisor_id=hypervisor["id"],
                    guest_id=vmid,
                    guest_name=guest_name,
                    guest_type=guest_type,
                    status="failed",
                    finished_at=tz.get_now(),
                    errors=[str(e)],
                )
                # Collect failed result for metadata
                backup_results.append(
                    {
                        "vmid": vmid,
                        "guest_name": guest_name,
                        "guest_type": guest_type,
                        "success": False,
                        "errors": [str(e)],
                    }
                )

        # Release storage reference (only deletes if no other tasks using it)
        # This unmounts the share when last task completes, keeping Proxmox UI clean
        if proxmox_storage_id:
            deleted = api.release_backer_storage(proxmox_storage_id)
            if deleted:
                logger.info(f"Removed temporary Proxmox storage '{proxmox_storage_id}'")

        # Finish job progress tracking
        _storage.update_job_progress(
            run_id=run_id,
            status="completed",
            progress_percent=100,
            message=f"Completed backup of {len(guest_ids)} guest(s)",
            files_processed=len(guest_ids),
        )
        _storage.finish_job_progress(run_id, "completed")

        logger.info(f"Scheduled hypervisor job '{job.get('name')}' completed")

        # Write metadata to repository
        try:
            _write_backup_metadata_to_repo(
                repository=repo_with_password,
                hypervisor=hypervisor,
                job=job,
                job_id=job_id,
                run_id=run_id,
                results=backup_results,
                guest_map=guest_map,
            )
        except Exception as meta_err:
            # Don't fail backup if metadata write fails
            logger.warning(f"Failed to write Proxmox backup metadata: {meta_err}")

    except Exception as e:
        logger.exception(f"Hypervisor job {job_id} failed: {e}")
        # Mark job progress as failed
        try:
            _storage.update_job_progress(
                run_id=run_id,
                status="failed",
                message=f"Job failed: {e}",
            )
            _storage.finish_job_progress(run_id, "failed")
        except Exception:
            pass  # Best effort
        # Release storage reference even on failure
        if proxmox_storage_id:
            try:
                api.release_backer_storage(proxmox_storage_id)
            except Exception:
                pass  # Best effort cleanup


def _trigger_hyperv_backup_job(job_id: str, job: dict, hypervisor: dict) -> None:
    """Trigger a Hyper-V backup job."""
    from backer.hypervisors.hyperv import HyperVAPI, HyperVBackupManager

    # Get repository for backup destination
    repository_id = job.get("repository_id")
    if not repository_id:
        logger.error(f"No repository configured for job: {job_id}")
        return

    repository = _storage.get_repository(repository_id)
    if not repository:
        logger.error(f"Repository not found: {repository_id}")
        return

    # Validate repository type (must be SMB for Hyper-V)
    repo_type = repository.get("repo_type", "").lower()
    if repo_type != "smb":
        logger.error(
            f"Repository type '{repo_type}' is not supported for Hyper-V backups. "
            "Use an SMB repository so the Hyper-V host can export directly to it."
        )
        return

    # Get credentials
    hv_password = _storage.get_hypervisor_password(hypervisor["id"])

    run_id = tz.get_now().strftime("%Y%m%d_%H%M%S_%f")

    try:
        # Get domain from hypervisor data (exposed from config by storage layer)
        # or fall back to checking config directly for backward compatibility
        domain = hypervisor.get("domain") or hypervisor.get("config", {}).get("domain")

        api = HyperVAPI(
            host=hypervisor["host"],
            username=hypervisor.get("username", "Administrator"),
            password=hv_password,
            port=hypervisor.get("port", 5985),
            use_ssl=hypervisor.get("port", 5985) == 5986,
            verify_ssl=hypervisor.get("verify_ssl", False),
            domain=domain,
        )

        backup_manager = HyperVBackupManager(api)

        # Get guest IDs from job
        guest_ids = job.get("guest_ids", [])

        if not guest_ids:
            # Backup all VMs
            guests = backup_manager.list_all_guests()
            guest_ids = [g["vmid"] for g in guests]

        # Build guest name map
        all_guests = backup_manager.list_all_guests()
        guest_map = {g["vmid"]: g for g in all_guests}

        # Build UNC path for export destination
        # SMB repository format: //server/share/path
        smb_server = repository.get("server", "")
        smb_share = repository.get("share", "")
        smb_path = repository.get("path", "")

        # Build UNC path that Windows can use
        backup_base_path = f"\\\\{smb_server}\\{smb_share}"
        if smb_path:
            smb_path_win = smb_path.replace("/", "\\")
            backup_base_path = f"{backup_base_path}\\{smb_path_win}"

        # Add hypervisor subfolder
        backup_base_path = f"{backup_base_path}\\Hypervisors\\{hypervisor['name']}"

        logger.info(
            f"Starting Hyper-V backup job '{job['name']}' to repository "
            f"'{repository['name']}' (path: {backup_base_path})"
        )

        # Get backup mode
        backup_mode = job.get("backup_mode", "online")

        # Get SMB credentials for authentication (required for WinRM double-hop)
        smb_username = repository.get("username", "")
        smb_password = _storage.get_storage_password(repository_id)
        smb_domain = repository.get("domain", "")

        # Start job progress tracking for activity panel
        _storage.start_job_progress(
            run_id=run_id,
            job_name=job["name"],
        )
        _storage.update_job_progress(
            run_id=run_id,
            status="running",
            message=f"Starting backup of {len(guest_ids)} VM(s)",
            total_files=len(guest_ids),
            files_processed=0,
        )

        # Collect results for metadata writing
        backup_results = []
        total_vms = len(guest_ids)

        for idx, vmid in enumerate(guest_ids):
            guest = guest_map.get(vmid)
            guest_name = guest.get("name", f"VM {vmid}") if guest else f"VM {vmid}"
            vm_num = idx + 1  # 1-based for display

            # Progress strategy for multi-VM jobs (Option C):
            # - Progress bar shows overall completion (VM count based)
            # - Text shows current phase with "VM X of Y" context
            # - Completed VMs count toward progress, current VM adds partial progress

            # Phase weights - how far through the current VM's backup we are
            # The "exporting" phase is the longest, taking most of the time
            phase_weights = {
                "starting": 0,
                "shutting_down": 5,
                "creating_checkpoint": 10,
                "exporting": 15,  # Stays here during the long export+copy
                "verifying": 85,
                "cleanup": 95,
                "starting_vm": 98,
                "completed": 100,
            }

            def hyperv_progress_callback(progress_info: dict) -> None:
                """Update job progress based on Hyper-V backup phase."""
                phase = progress_info.get("status", "")
                vm = progress_info.get("vm", guest_name)

                phase_pct = phase_weights.get(phase, 0)

                # Calculate overall progress:
                # (completed_vms + current_vm_progress) / total_vms * 100
                completed_vms = idx  # VMs completed before this one
                current_vm_progress = phase_pct / 100.0  # 0.0 to 1.0
                overall_pct = int(((completed_vms + current_vm_progress) / total_vms) * 100)
                overall_pct = min(overall_pct, 99)  # Never show 100% until truly done

                # Human-readable phase messages with VM count context
                vm_context = f"[{vm_num}/{total_vms}] " if total_vms > 1 else ""

                phase_messages = {
                    "starting": f"{vm_context}Starting: {vm}",
                    "shutting_down": f"{vm_context}Shutting down: {vm}",
                    "creating_checkpoint": f"{vm_context}Creating checkpoint: {vm}",
                    "exporting": f"{vm_context}Exporting & copying: {vm}",
                    "verifying": f"{vm_context}Verifying: {vm}",
                    "cleanup": f"{vm_context}Cleaning up: {vm}",
                    "starting_vm": f"{vm_context}Restarting: {vm}",
                    "completed": f"{vm_context}Completed: {vm}",
                }
                message = phase_messages.get(phase, f"{vm_context}Backing up: {vm}")

                _storage.update_job_progress(
                    run_id=run_id,
                    status="running",
                    progress_percent=overall_pct,
                    message=message,
                    current_file=vm,
                    files_processed=idx,
                    total_files=total_vms,
                )

            # Initial progress update for this VM
            base_pct = int((idx / total_vms) * 100)
            vm_context = f"[{vm_num}/{total_vms}] " if total_vms > 1 else ""
            _storage.update_job_progress(
                run_id=run_id,
                status="running",
                progress_percent=base_pct,
                message=f"{vm_context}Starting: {guest_name}",
                current_file=guest_name,
                files_processed=idx,
                total_files=total_vms,
            )

            # Save run as running
            _storage.save_hypervisor_run(
                run_id=run_id,
                job_id=job_id,
                job_name=job["name"],
                hypervisor_id=hypervisor["id"],
                guest_id=vmid,
                guest_name=guest_name,
                guest_type="vm",
                status="running",
            )

            try:
                result = backup_manager.backup_vm(
                    vm_name=guest_name,
                    backup_path=backup_base_path,
                    backup_mode=backup_mode,
                    progress_callback=hyperv_progress_callback,
                    smb_username=smb_username,
                    smb_password=smb_password,
                    smb_domain=smb_domain,
                )

                backup_size = result.get("size_bytes", 0)
                backup_filename = result.get("export_path")

                # Update run record
                _storage.save_hypervisor_run(
                    run_id=run_id,
                    job_id=job_id,
                    job_name=job["name"],
                    hypervisor_id=hypervisor["id"],
                    guest_id=vmid,
                    guest_name=guest_name,
                    guest_type="vm",
                    status="success" if result.get("success") else "failed",
                    finished_at=tz.get_now(),
                    duration_seconds=result.get("duration_seconds"),
                    backup_size=backup_size,
                    backup_filename=backup_filename,
                    errors=result.get("errors"),
                )

                # Collect result for metadata
                backup_results.append(
                    {
                        "vmid": vmid,
                        "guest_name": guest_name,
                        "guest_type": "vm",
                        "success": result.get("success", False),
                        "backup_filename": backup_filename,
                        "backup_size": backup_size,
                        "duration_seconds": result.get("duration_seconds"),
                        "started_at": tz.get_now().isoformat(),
                        "finished_at": tz.get_now().isoformat(),
                        "errors": result.get("errors"),
                    }
                )

                if result.get("success"):
                    logger.info(f"Backup succeeded for VM '{guest_name}' ({backup_size / 1024 / 1024:.1f} MB)")

                    # Enforce copies_to_keep retention after successful backup
                    copies_to_keep = job.get("copies_to_keep", 0)
                    if copies_to_keep > 0:
                        logger.info(f"Enforcing copies_to_keep={copies_to_keep} for VM {guest_name}")
                        try:
                            repo_password = _storage.get_storage_password(repository_id)
                            repo_with_password = {**repository, "password": repo_password}
                            _enforce_copies_limit(
                                repository=repo_with_password,
                                hypervisor_name=hypervisor["name"],
                                vmid=guest_name,  # Hyper-V uses VM name as ID
                                copies_to_keep=copies_to_keep,
                                hypervisor_type="hyperv",
                            )
                        except Exception as e:
                            logger.warning(f"Failed to enforce retention for VM {guest_name}: {e}")
                            # Don't fail backup if retention enforcement fails
                else:
                    logger.warning(f"Backup failed for VM '{guest_name}': {result.get('errors')}")

            except Exception as e:
                logger.exception(f"Backup failed for VM '{guest_name}': {e}")
                _storage.save_hypervisor_run(
                    run_id=run_id,
                    job_id=job_id,
                    job_name=job["name"],
                    hypervisor_id=hypervisor["id"],
                    guest_id=vmid,
                    guest_name=guest_name,
                    guest_type="vm",
                    status="failed",
                    finished_at=tz.get_now(),
                    errors=[str(e)],
                )
                # Collect failed result for metadata
                backup_results.append(
                    {
                        "vmid": vmid,
                        "guest_name": guest_name,
                        "guest_type": "vm",
                        "success": False,
                        "errors": [str(e)],
                    }
                )

        # Finish job progress tracking
        success_count = sum(1 for r in backup_results if r.get("success"))
        fail_count = len(backup_results) - success_count
        final_status = "completed" if fail_count == 0 else "failed" if success_count == 0 else "completed"
        _storage.update_job_progress(
            run_id=run_id,
            status=final_status,
            progress_percent=100,
            message=f"Completed: {success_count} succeeded, {fail_count} failed",
            files_processed=len(guest_ids),
        )
        _storage.finish_job_progress(run_id, final_status)

        logger.info(f"Hyper-V backup job '{job.get('name')}' completed")

        # Write metadata to repository
        try:
            # Get repository password for metadata writing
            repo_password = _storage.get_storage_password(repository_id)
            repo_with_password = {**repository, "password": repo_password}

            _write_backup_metadata_to_repo(
                repository=repo_with_password,
                hypervisor=hypervisor,
                job=job,
                job_id=job_id,
                run_id=run_id,
                results=backup_results,
                guest_map=guest_map,
            )
        except Exception as meta_err:
            # Don't fail backup if metadata write fails
            logger.warning(f"Failed to write Hyper-V backup metadata: {meta_err}")

    except Exception as e:
        logger.exception(f"Hyper-V job {job_id} failed: {e}")
        # Mark job progress as failed
        try:
            _storage.update_job_progress(
                run_id=run_id,
                status="failed",
                message=f"Job failed: {e}",
            )
            _storage.finish_job_progress(run_id, "failed")
        except Exception:
            pass  # Best effort cleanup


def _trigger_hyperv_cluster_backup_job(job_id: str, job: dict, hypervisor: dict) -> None:
    """Trigger a Hyper-V Cluster backup job.

    Similar to _trigger_hyperv_backup_job but uses cluster-aware API
    that routes to the correct owner node for each VM.
    """
    from backer.hypervisors.hyperv import HyperVClusterAPI, HyperVClusterBackupManager

    # Get repository for backup destination
    repository_id = job.get("repository_id")
    if not repository_id:
        logger.error(f"No repository configured for job: {job_id}")
        return

    repository = _storage.get_repository(repository_id)
    if not repository:
        logger.error(f"Repository not found: {repository_id}")
        return

    # Validate repository type (must be SMB for Hyper-V)
    repo_type = repository.get("repo_type", "").lower()
    if repo_type != "smb":
        logger.error(
            f"Repository type '{repo_type}' is not supported for Hyper-V backups. "
            "Use an SMB repository so the Hyper-V host can export directly to it."
        )
        return

    # Get credentials
    hv_password = _storage.get_hypervisor_password(hypervisor["id"])

    run_id = tz.get_now().strftime("%Y%m%d_%H%M%S_%f")

    try:
        # Get domain and cluster_name from hypervisor data
        domain = hypervisor.get("domain") or hypervisor.get("config", {}).get("domain")
        cluster_name = hypervisor.get("cluster_name") or hypervisor.get("config", {}).get("cluster_name")

        api = HyperVClusterAPI(
            host=hypervisor["host"],
            username=hypervisor.get("username", "Administrator"),
            password=hv_password,
            cluster_name=cluster_name,
            port=hypervisor.get("port", 5985),
            use_ssl=hypervisor.get("port", 5985) == 5986,
            verify_ssl=hypervisor.get("verify_ssl", False),
            domain=domain,
        )

        backup_manager = HyperVClusterBackupManager(api)

        # Get guest IDs from job
        guest_ids = job.get("guest_ids", [])

        if not guest_ids:
            # Backup all VMs
            guests = backup_manager.list_all_guests()
            guest_ids = [g["vmid"] for g in guests]

        # Build guest name map
        all_guests = backup_manager.list_all_guests()
        guest_map = {g["vmid"]: g for g in all_guests}

        # Build UNC path for export destination
        smb_server = repository.get("server", "")
        smb_share = repository.get("share", "")
        smb_path = repository.get("path", "")

        backup_base_path = f"\\\\{smb_server}\\{smb_share}"
        if smb_path:
            smb_path_win = smb_path.replace("/", "\\")
            backup_base_path = f"{backup_base_path}\\{smb_path_win}"

        # Add hypervisor subfolder
        backup_base_path = f"{backup_base_path}\\Hypervisors\\{hypervisor['name']}"

        logger.info(
            f"Starting Hyper-V Cluster backup job '{job['name']}' to repository "
            f"'{repository['name']}' (path: {backup_base_path})"
        )

        # Get backup mode
        backup_mode = job.get("backup_mode", "online")

        # Get SMB credentials
        smb_username = repository.get("username", "")
        smb_password = _storage.get_storage_password(repository_id)
        smb_domain = repository.get("domain", "")

        # Start job progress tracking
        _storage.start_job_progress(
            run_id=run_id,
            job_name=job["name"],
        )
        _storage.update_job_progress(
            run_id=run_id,
            status="running",
            message=f"Starting backup of {len(guest_ids)} VM(s) from cluster",
            total_files=len(guest_ids),
            files_processed=0,
        )

        backup_results = []
        total_vms = len(guest_ids)

        for idx, vmid in enumerate(guest_ids):
            guest = guest_map.get(vmid)
            guest_name = guest.get("name", f"VM {vmid}") if guest else f"VM {vmid}"
            owner_node = guest.get("owner_node", "unknown") if guest else "unknown"
            vm_num = idx + 1

            phase_weights = {
                "starting": 0,
                "shutting_down": 5,
                "creating_checkpoint": 10,
                "exporting": 15,
                "verifying": 85,
                "cleanup": 95,
                "starting_vm": 98,
                "completed": 100,
            }

            def hyperv_cluster_progress_callback(progress_info: dict) -> None:
                """Update job progress based on Hyper-V Cluster backup phase."""
                phase = progress_info.get("status", "")
                vm = progress_info.get("vm", guest_name)
                node = progress_info.get("node", owner_node)

                phase_pct = phase_weights.get(phase, 0)
                completed_vms = idx
                current_vm_progress = phase_pct / 100.0
                overall_pct = int(((completed_vms + current_vm_progress) / total_vms) * 100)
                overall_pct = min(overall_pct, 99)

                vm_context = f"[{vm_num}/{total_vms}] " if total_vms > 1 else ""

                phase_messages = {
                    "starting": f"{vm_context}Starting: {vm} (node: {node})",
                    "shutting_down": f"{vm_context}Shutting down: {vm}",
                    "creating_checkpoint": f"{vm_context}Creating checkpoint: {vm}",
                    "exporting": f"{vm_context}Exporting & copying: {vm}",
                    "verifying": f"{vm_context}Verifying: {vm}",
                    "cleanup": f"{vm_context}Cleaning up: {vm}",
                    "starting_vm": f"{vm_context}Restarting: {vm}",
                    "completed": f"{vm_context}Completed: {vm}",
                }
                message = phase_messages.get(phase, f"{vm_context}Backing up: {vm}")

                _storage.update_job_progress(
                    run_id=run_id,
                    status="running",
                    progress_percent=overall_pct,
                    message=message,
                    current_file=vm,
                    files_processed=idx,
                    total_files=total_vms,
                )

            # Initial progress update
            base_pct = int((idx / total_vms) * 100)
            vm_context = f"[{vm_num}/{total_vms}] " if total_vms > 1 else ""
            _storage.update_job_progress(
                run_id=run_id,
                status="running",
                progress_percent=base_pct,
                message=f"{vm_context}Starting: {guest_name} (node: {owner_node})",
                current_file=guest_name,
                files_processed=idx,
                total_files=total_vms,
            )

            # Save run as running
            _storage.save_hypervisor_run(
                run_id=run_id,
                job_id=job_id,
                job_name=job["name"],
                hypervisor_id=hypervisor["id"],
                guest_id=vmid,
                guest_name=guest_name,
                guest_type="vm",
                status="running",
            )

            try:
                result = backup_manager.backup_vm(
                    vm_name=guest_name,
                    backup_path=backup_base_path,
                    backup_mode=backup_mode,
                    progress_callback=hyperv_cluster_progress_callback,
                    smb_username=smb_username,
                    smb_password=smb_password,
                    smb_domain=smb_domain,
                )

                backup_size = result.get("size_bytes", 0)
                backup_filename = result.get("export_path")

                _storage.save_hypervisor_run(
                    run_id=run_id,
                    job_id=job_id,
                    job_name=job["name"],
                    hypervisor_id=hypervisor["id"],
                    guest_id=vmid,
                    guest_name=guest_name,
                    guest_type="vm",
                    status="success" if result.get("success") else "failed",
                    finished_at=tz.get_now(),
                    duration_seconds=result.get("duration_seconds"),
                    backup_size=backup_size,
                    backup_filename=backup_filename,
                    errors=result.get("errors"),
                )

                backup_results.append(
                    {
                        "vmid": vmid,
                        "guest_name": guest_name,
                        "guest_type": "vm",
                        "success": result.get("success", False),
                        "backup_filename": backup_filename,
                        "backup_size": backup_size,
                        "duration_seconds": result.get("duration_seconds"),
                        "started_at": tz.get_now().isoformat(),
                        "finished_at": tz.get_now().isoformat(),
                        "errors": result.get("errors"),
                        "owner_node": result.get("owner_node"),
                    }
                )

                if result.get("success"):
                    logger.info(
                        f"Cluster backup succeeded for VM '{guest_name}' "
                        f"on node '{result.get('owner_node')}' "
                        f"({backup_size / 1024 / 1024:.1f} MB)"
                    )

                    # Enforce retention
                    copies_to_keep = job.get("copies_to_keep", 0)
                    if copies_to_keep > 0:
                        logger.info(f"Enforcing copies_to_keep={copies_to_keep} for VM {guest_name}")
                        try:
                            repo_password = _storage.get_storage_password(repository_id)
                            repo_with_password = {**repository, "password": repo_password}
                            _enforce_copies_limit(
                                repository=repo_with_password,
                                hypervisor_name=hypervisor["name"],
                                vmid=guest_name,
                                copies_to_keep=copies_to_keep,
                                hypervisor_type="hyperv",
                            )
                        except Exception as e:
                            logger.warning(f"Failed to enforce retention for VM {guest_name}: {e}")
                else:
                    logger.warning(f"Cluster backup failed for VM '{guest_name}': {result.get('errors')}")

            except Exception as e:
                logger.exception(f"Cluster backup failed for VM '{guest_name}': {e}")
                _storage.save_hypervisor_run(
                    run_id=run_id,
                    job_id=job_id,
                    job_name=job["name"],
                    hypervisor_id=hypervisor["id"],
                    guest_id=vmid,
                    guest_name=guest_name,
                    guest_type="vm",
                    status="failed",
                    finished_at=tz.get_now(),
                    errors=[str(e)],
                )
                backup_results.append(
                    {
                        "vmid": vmid,
                        "guest_name": guest_name,
                        "guest_type": "vm",
                        "success": False,
                        "errors": [str(e)],
                    }
                )

        # Finish job progress tracking
        success_count = sum(1 for r in backup_results if r.get("success"))
        fail_count = len(backup_results) - success_count
        final_status = "completed" if fail_count == 0 else "failed" if success_count == 0 else "completed"
        _storage.update_job_progress(
            run_id=run_id,
            status=final_status,
            progress_percent=100,
            message=f"Completed: {success_count} succeeded, {fail_count} failed",
            files_processed=len(guest_ids),
        )
        _storage.finish_job_progress(run_id, final_status)

        logger.info(f"Hyper-V Cluster backup job '{job.get('name')}' completed")

        # Write metadata to repository
        try:
            repo_password = _storage.get_storage_password(repository_id)
            repo_with_password = {**repository, "password": repo_password}

            _write_backup_metadata_to_repo(
                repository=repo_with_password,
                hypervisor=hypervisor,
                job=job,
                job_id=job_id,
                run_id=run_id,
                results=backup_results,
                guest_map=guest_map,
            )
        except Exception as meta_err:
            logger.warning(f"Failed to write Hyper-V Cluster backup metadata: {meta_err}")

    except Exception as e:
        logger.exception(f"Hyper-V Cluster job {job_id} failed: {e}")
        try:
            _storage.update_job_progress(
                run_id=run_id,
                status="failed",
                message=f"Job failed: {e}",
            )
            _storage.finish_job_progress(run_id, "failed")
        except Exception:
            pass


# ============================================================================
# Module-level metadata writing functions
# These are used by the scheduled job trigger functions above
# ============================================================================


def _write_metadata_nfs_ml(
    server: str,
    export: str,
    hypervisor_name: str,
    backer_dir: Path,
) -> None:
    """Write metadata to NFS share by temporarily mounting it (module-level version)."""
    import subprocess
    import tempfile

    mount_point = tempfile.mkdtemp(prefix="backer_nfs_meta_")

    try:
        # Mount the NFS export
        mount_cmd = [
            "sudo",
            "-n",
            "mount",
            "-t",
            "nfs",
            "-o",
            "soft,timeo=50,retrans=2",
            f"{server}:{export}",
            mount_point,
        ]
        result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            logger.warning(f"Failed to mount NFS for metadata write: {result.stderr.strip()}")
            return

        # Build target path: {mount}/Hypervisors/{hypervisor_name}/.backer
        target_backer = f"{mount_point}/Hypervisors/{hypervisor_name}/.backer"

        try:
            # Create target directory
            mkdir_result = subprocess.run(
                ["mkdir", "-p", target_backer],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if mkdir_result.returncode != 0:
                logger.warning(f"Failed to create metadata directory: {mkdir_result.stderr.strip()}")
                return

            # Copy all files from backer_dir to target
            cp_result = subprocess.run(
                ["cp", "-r", f"{backer_dir}/.", target_backer],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if cp_result.returncode != 0:
                logger.warning(f"Failed to copy metadata files: {cp_result.stderr.strip()}")
                return

            logger.info(f"Wrote hypervisor backup metadata to {server}:{export}/Hypervisors/{hypervisor_name}")

        except subprocess.TimeoutExpired:
            logger.warning("Timeout writing metadata files to NFS")

    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout mounting NFS {server}:{export} for metadata")
    finally:
        # Unmount
        try:
            subprocess.run(["sudo", "-n", "umount", mount_point], capture_output=True, timeout=30)
        except Exception:
            try:
                subprocess.run(["sudo", "-n", "umount", "-l", mount_point], capture_output=True, timeout=10)
            except Exception:
                pass

        # Remove temp mount point
        try:
            import os

            os.rmdir(mount_point)
        except Exception:
            pass


def _write_metadata_smb_ml(
    server: str,
    share: str,
    subdir: str,
    hypervisor_name: str,
    username: str | None,
    password: str | None,
    domain: str | None,
    backer_dir: Path,
) -> None:
    """Write metadata to SMB share using smbclient (module-level version)."""
    import subprocess

    # Build remote path: {repo_path}/Hypervisors/{hypervisor_name}/
    base_path = subdir.strip("/") if subdir else ""
    if base_path:
        remote_base = f"{base_path}/Hypervisors/{hypervisor_name}"
    else:
        remote_base = f"Hypervisors/{hypervisor_name}"

    # Build smbclient auth
    auth_parts = []
    if username:
        auth_parts.extend(["-U", f"{domain}\\{username}%{password}" if domain else f"{username}%{password}"])
    else:
        auth_parts.extend(["-N"])  # No password

    # Track directories we've already created
    created_dirs: set[str] = set()

    def ensure_remote_dir(dir_path: str) -> None:
        """Create remote directory and all parents using smbclient."""
        if not dir_path or dir_path in created_dirs:
            return

        parts = dir_path.split("/")
        for i in range(1, len(parts) + 1):
            partial_path = "/".join(parts[:i])
            if partial_path and partial_path not in created_dirs:
                mkdir_cmd = ["smbclient", f"//{server}/{share}", *auth_parts, "-c", f"mkdir {partial_path}"]
                subprocess.run(mkdir_cmd, capture_output=True, timeout=30)
                created_dirs.add(partial_path)

    # Upload each file in .backer directory
    for local_file in backer_dir.rglob("*"):
        if not local_file.is_file():
            continue

        rel_path = local_file.relative_to(backer_dir)
        rel_path_str = str(rel_path).replace("\\", "/")

        parent_str = str(rel_path.parent).replace("\\", "/")
        if parent_str == ".":
            remote_dir = f"{remote_base}/.backer"
        else:
            remote_dir = f"{remote_base}/.backer/{parent_str}"

        ensure_remote_dir(remote_dir)

        remote_file = f"{remote_base}/.backer/{rel_path_str}"
        put_cmd = ["smbclient", f"//{server}/{share}", *auth_parts, "-c", f"put {local_file} {remote_file}"]
        result = subprocess.run(put_cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            logger.debug(f"smbclient put failed for {remote_file}: {result.stderr.decode()}")
        else:
            logger.debug(f"Uploaded metadata: {remote_file}")

    logger.info(f"Wrote hypervisor backup metadata to //{server}/{share}/{remote_base}")


def _write_metadata_local(
    local_path: str,
    hypervisor_name: str,
    backer_dir: Path,
) -> None:
    """Write metadata directly to local filesystem for LOCAL repos."""
    import shutil

    try:
        # Build target path: {local_path}/Hypervisors/{hypervisor_name}/.backer
        target_base = Path(local_path) / "Hypervisors" / hypervisor_name
        target_backer = target_base / ".backer"

        # Create target directory structure
        target_backer.mkdir(parents=True, exist_ok=True)

        # Copy all files from backer_dir to target
        # Walk through source and copy each file, preserving directory structure
        for src_file in backer_dir.rglob("*"):
            if src_file.is_file():
                # Get relative path from backer_dir
                rel_path = src_file.relative_to(backer_dir)
                dst_file = target_backer / rel_path

                # Ensure parent directory exists
                dst_file.parent.mkdir(parents=True, exist_ok=True)

                # Copy the file
                shutil.copy2(src_file, dst_file)
                logger.debug(f"Copied metadata: {rel_path}")

        logger.info(f"Wrote hypervisor backup metadata to {target_backer}")

    except Exception as e:
        logger.warning(f"Failed to write metadata to local path {local_path}: {e}")


def _write_backup_metadata_to_repo(
    repository: dict[str, Any],
    hypervisor: dict[str, Any],
    job: dict[str, Any],
    job_id: str,
    run_id: str,
    results: list[dict[str, Any]],
    guest_map: dict[str, Any],
) -> None:
    """Write backup metadata to the repository (module-level version).

    This allows the metadata to be discovered if the Backer server is reinstalled.
    Used by scheduled backup trigger functions.

    Args:
        repository: Repository dict (with password already included)
        hypervisor: Hypervisor dict
        job: Job configuration dict
        job_id: Job ID
        run_id: Run ID
        results: List of backup result dicts
        guest_map: Dict mapping guest_id to guest info dict
    """
    import tempfile

    from backer.hypervisors.metadata import HypervisorMetadata

    repo_type = repository.get("repo_type", "").lower()
    if repo_type not in ("smb", "nfs", "local"):
        logger.debug(f"Skipping metadata write for repo type: {repo_type}")
        return

    # For LOCAL repos, get the local path
    local_path = repository.get("share") or repository.get("path", "")

    # For SMB/NFS repos, get network details
    server = repository.get("server", "")
    share = repository.get("share", "")
    subdir = repository.get("path", "")
    username = repository.get("username")
    password = repository.get("password")
    domain = repository.get("domain")

    # Validate required fields based on repo type
    if repo_type == "local":
        if not local_path:
            logger.warning("Cannot write metadata: missing local path for LOCAL repo")
            return
    elif not server or not share:
        logger.warning("Cannot write metadata: missing server or share")
        return

    # Sanitize hypervisor name for folder
    safe_hv_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in hypervisor["name"])

    # Create metadata in a temp directory first
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        metadata = HypervisorMetadata(tmp_path)

        # Initialize if needed
        if not metadata.is_initialized():
            metadata.initialize()

        # Save hypervisor info
        metadata.save_hypervisor(
            hypervisor_id=hypervisor["id"],
            name=hypervisor["name"],
            hypervisor_type=hypervisor.get("hypervisor_type", "proxmox"),
            host=hypervisor["host"],
        )

        # Save job configuration (for discovery on reinstall)
        metadata.save_job(
            job_id=job_id,
            name=job["name"],
            hypervisor_id=hypervisor["id"],
            repository_id=job.get("repository_id", ""),
            guest_ids=job.get("guest_ids"),
            backup_mode=job.get("backup_mode", "snapshot"),
            compression=job.get("compression", "zstd"),
            schedule_cron=job.get("schedule_cron"),
            enabled=job.get("enabled", True),
            copies_to_keep=job.get("copies_to_keep", 0),
            hypervisor_name=hypervisor["name"],
            hypervisor_host=hypervisor["host"],
        )

        # Save guest and run info for each result
        for result in results:
            vmid = result.get("vmid") or result.get("guest_id")
            if not vmid:
                continue

            guest = guest_map.get(vmid) or guest_map.get(str(vmid))

            # Handle both dict guests (Hyper-V/Unraid) and object guests (Proxmox)
            if guest:
                if hasattr(guest, "name"):
                    guest_name = guest.name
                else:
                    guest_name = guest.get("name", f"Guest {vmid}")

                if hasattr(guest, "guest_type"):
                    guest_type = guest.guest_type.value if hasattr(guest.guest_type, "value") else str(guest.guest_type)
                else:
                    guest_type = guest.get("guest_type", "vm")

                if hasattr(guest, "node"):
                    node = guest.node
                else:
                    node = guest.get("node", hypervisor["host"])
            else:
                guest_name = result.get("guest_name", f"Guest {vmid}")
                guest_type = result.get("guest_type", "vm")
                node = hypervisor["host"]

            # Save guest info (vmid needs to be int for save_guest)
            vmid_int = int(vmid) if isinstance(vmid, str) and vmid.isdigit() else hash(str(vmid)) % (10**9)
            metadata.save_guest(
                vmid=vmid_int,
                name=guest_name,
                guest_type=guest_type,
                node=node,
                hypervisor_id=hypervisor["id"],
            )

            # Save run record
            metadata.save_backup_run(
                vmid=vmid_int,
                run_id=run_id,
                status="success" if result.get("success") else "failed",
                backup_file=(
                    result.get("archive_name") or result.get("backup_filename") or result.get("export_path", "")
                ),
                started_at=result.get("started_at", tz.get_now().isoformat()),
                finished_at=result.get("finished_at"),
                size_bytes=result.get("archive_size") or result.get("backup_size"),
                duration_seconds=result.get("duration_seconds"),
                backup_type=result.get("backup_type", "full"),
                skipped=result.get("skipped", False),
                job_name=job["name"],
                job_id=job_id,
                hypervisor_id=hypervisor["id"],
            )

        # Now upload the .backer directory to the share/local path
        backer_dir = tmp_path / ".backer"
        if not backer_dir.exists():
            return

        if repo_type == "local":
            _write_metadata_local(
                local_path=local_path,
                hypervisor_name=safe_hv_name,
                backer_dir=backer_dir,
            )
        elif repo_type == "nfs":
            _write_metadata_nfs_ml(
                server=server,
                export=share,
                hypervisor_name=safe_hv_name,
                backer_dir=backer_dir,
            )
        else:
            _write_metadata_smb_ml(
                server=server,
                share=share,
                subdir=subdir,
                hypervisor_name=safe_hv_name,
                username=username,
                password=password,
                domain=domain,
                backer_dir=backer_dir,
            )


def verify_client(
    credentials: HTTPBasicCredentials | None = Depends(security),
    storage: Storage = Depends(get_storage),
) -> Client:
    """Verify client credentials and return client info."""
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    client = storage.get_client(credentials.username)
    if not client:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid client ID")

    secret_hash = storage.get_client_secret_hash(credentials.username)
    provided_hash = hashlib.sha256(credentials.password.encode()).hexdigest()

    if not secrets.compare_digest(secret_hash or "", provided_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    return client


def _enforce_copies_limit(
    repository: dict[str, Any],
    hypervisor_name: str,
    vmid: int | str,
    copies_to_keep: int,
    hypervisor_type: str = "proxmox",
) -> None:
    """Enforce the copies_to_keep limit by deleting oldest backups.

    After a successful backup, this function checks how many backups exist
    for the VM and deletes the oldest ones to stay within the limit.

    Args:
        repository: Repository configuration dict
        hypervisor_name: Name of the hypervisor
        vmid: VM ID (int for Proxmox, str VM name for Hyper-V)
        copies_to_keep: Number of backups to keep (0 = unlimited, delete nothing)
        hypervisor_type: Type of hypervisor ("proxmox" or "hyperv")

    For Proxmox:
        Files are named: vzdump-{type}-{vmid}-{YYYY_MM_DD-HH_MM_SS}.vma.{zst|gz|lzo}
        Also deletes associated .notes and .log files.
        Location: {repo_path}/Hypervisors/{hypervisor_name}/dump/

    For Hyper-V:
        Backup folders: {repo_path}/Hypervisors/{hypervisor_name}/{vm_name}/{timestamp}/
        Deletes entire timestamp folders.
    """
    if copies_to_keep <= 0:
        # 0 means unlimited - don't delete anything
        return

    repo_type = repository.get("repo_type", "").lower()
    if repo_type not in ("smb", "nfs"):
        logger.debug(f"copies_to_keep enforcement not supported for repo type: {repo_type}")
        return

    # Sanitize hypervisor name for folder
    safe_hv_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in hypervisor_name)

    if hypervisor_type in ("hyperv", "hyperv-cluster"):
        if repo_type == "smb":
            _enforce_copies_limit_hyperv_smb(repository, safe_hv_name, str(vmid), copies_to_keep)
        # NFS not typically used with Hyper-V but could be added
    else:
        # Proxmox (default)
        if repo_type == "smb":
            _enforce_copies_limit_smb(repository, safe_hv_name, int(vmid), copies_to_keep)
        elif repo_type == "nfs":
            _enforce_copies_limit_nfs(repository, safe_hv_name, int(vmid), copies_to_keep)


def _enforce_copies_limit_smb(
    repository: dict[str, Any],
    hypervisor_name: str,
    vmid: int,
    copies_to_keep: int,
) -> None:
    """Enforce backup copies limit for a VM on SMB share by deleting oldest backups."""
    import re
    import subprocess

    server = repository.get("server", "")
    share = repository.get("share", "")
    subdir = repository.get("path", "").strip("/")
    username = repository.get("username")
    password = repository.get("password")
    domain = repository.get("domain")

    if not server or not share:
        return

    # Build path to dump directory
    base_path = f"{subdir}/Hypervisors/{hypervisor_name}/dump" if subdir else f"Hypervisors/{hypervisor_name}/dump"

    # Build smbclient auth
    auth_opts = []
    if username:
        if password:
            auth_opts = ["-U", f"{domain}\\{username}%{password}" if domain else f"{username}%{password}"]
        else:
            auth_opts = ["-U", f"{domain}\\{username}" if domain else username]
    else:
        auth_opts = ["-N"]

    # List all files matching the VMID pattern
    # vzdump-qemu-{vmid}-* or vzdump-lxc-{vmid}-*
    list_cmd = ["smbclient", f"//{server}/{share}", *auth_opts, "-c", f"cd {base_path}; ls"]
    try:
        result = subprocess.run(list_cmd, capture_output=True, timeout=60, text=True)
        if result.returncode != 0:
            logger.debug(f"Could not list files in {base_path}: {result.stderr}")
            return

        # Parse file listing and find backup files for this VMID
        # Pattern: vzdump-{qemu|lxc}-{vmid}-{YYYY_MM_DD-HH_MM_SS}.vma.{zst|gz|lzo}
        # Must end with .vma, .vma.zst, .vma.gz, or .vma.lzo (not .notes or .log)
        lines = result.stdout.strip().split("\n")
        backup_files: list[tuple[str, str]] = []  # (timestamp, filename)
        timestamp_pattern = re.compile(r"vzdump-(?:qemu|lxc)-\d+-(\d{4}_\d{2}_\d{2}-\d{2}_\d{2}_\d{2})\.vma")
        # Only match actual backup files, not .notes or .log
        backup_ext_pattern = re.compile(r"\.vma(\.zst|\.gz|\.lzo)?$")

        for line in lines:
            parts = line.strip().split()
            if not parts:
                continue
            filename = parts[0]
            # Match vzdump-{type}-{vmid}-{timestamp}.vma* (but not .notes or .log)
            if (
                filename.startswith(f"vzdump-qemu-{vmid}-") or filename.startswith(f"vzdump-lxc-{vmid}-")
            ) and backup_ext_pattern.search(filename):
                # Extract timestamp for sorting
                match = timestamp_pattern.search(filename)
                if match:
                    timestamp = match.group(1)
                    backup_files.append((timestamp, filename))

        # Sort by timestamp (oldest first)
        backup_files.sort(key=lambda x: x[0])

        current_count = len(backup_files)
        logger.info(f"VM {vmid}: Found {current_count} backups, limit is {copies_to_keep}")

        if current_count <= copies_to_keep:
            logger.debug(f"VM {vmid}: {current_count} backups <= {copies_to_keep} limit, nothing to delete")
            return

        # Delete oldest backups to stay within limit
        backups_to_delete = current_count - copies_to_keep
        deleted_count = 0

        for i in range(backups_to_delete):
            timestamp, backup_file = backup_files[i]
            files_to_delete = [backup_file]

            # Also delete associated .log and .notes files
            base_name = (
                backup_file.replace(".vma.zst", "").replace(".vma.gz", "").replace(".vma.lzo", "").replace(".vma", "")
            )
            files_to_delete.append(f"{base_name}.log")
            files_to_delete.append(f"{backup_file}.notes")

            for filename in files_to_delete:
                smb_del_cmd = f'cd {base_path}; del "{filename}"'
                del_cmd = ["smbclient", f"//{server}/{share}", *auth_opts, "-c", smb_del_cmd]
                del_result = subprocess.run(del_cmd, capture_output=True, timeout=30)
                if del_result.returncode == 0:
                    logger.info(f"Deleted old backup file: {filename}")
                    if filename == backup_file:
                        deleted_count += 1
                else:
                    # Ignore errors for .log/.notes files that may not exist
                    if filename == backup_file:
                        err = del_result.stderr.decode() if del_result.stderr else "unknown error"
                        logger.warning(f"Failed to delete {filename}: {err}")

        if deleted_count > 0:
            logger.info(
                f"Retention enforcement: deleted {deleted_count} old backups for VM {vmid}, keeping {copies_to_keep}"
            )

    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout enforcing retention for VM {vmid} from SMB")
    except Exception as e:
        logger.warning(f"Error enforcing retention for VM {vmid} from SMB: {e}")


def _enforce_copies_limit_nfs(
    repository: dict[str, Any],
    hypervisor_name: str,
    vmid: int,
    copies_to_keep: int,
) -> None:
    """Enforce backup copies limit for a VM on NFS share by deleting oldest backups."""
    import re
    import subprocess
    import tempfile

    server = repository.get("server", "")
    export = repository.get("share", "")  # NFS export path
    subdir = repository.get("path", "").strip("/")

    if not server or not export:
        return

    # Create temp mount point
    mount_point = tempfile.mkdtemp(prefix="backer_nfs_del_")

    try:
        # Mount the NFS share
        mount_cmd = ["sudo", "-n", "mount", "-t", "nfs", f"{server}:{export}", mount_point]
        result = subprocess.run(mount_cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            err = result.stderr.decode() if result.stderr else "unknown error"
            logger.warning(f"Failed to mount NFS for retention enforcement: {err}")
            return

        # Build path to dump directory
        if subdir:
            dump_dir = Path(mount_point) / subdir / "Hypervisors" / hypervisor_name / "dump"
        else:
            dump_dir = Path(mount_point) / "Hypervisors" / hypervisor_name / "dump"

        if not dump_dir.exists():
            logger.debug(f"Dump directory does not exist: {dump_dir}")
            return

        # Find all backup files for this VMID with timestamps
        # Pattern: vzdump-{qemu|lxc}-{vmid}-{YYYY_MM_DD-HH_MM_SS}.vma.{zst|gz|lzo}
        # Must end with .vma, .vma.zst, .vma.gz, or .vma.lzo (not .notes or .log)
        timestamp_pattern = re.compile(r"vzdump-(?:qemu|lxc)-\d+-(\d{4}_\d{2}_\d{2}-\d{2}_\d{2}_\d{2})\.vma")
        backup_ext_pattern = re.compile(r"\.vma(\.zst|\.gz|\.lzo)?$")
        backup_files: list[tuple[str, Path]] = []  # (timestamp, path)

        for entry in dump_dir.iterdir():
            if not entry.is_file():
                continue
            if (
                entry.name.startswith(f"vzdump-qemu-{vmid}-") or entry.name.startswith(f"vzdump-lxc-{vmid}-")
            ) and backup_ext_pattern.search(entry.name):
                match = timestamp_pattern.search(entry.name)
                if match:
                    timestamp = match.group(1)
                    backup_files.append((timestamp, entry))

        # Sort by timestamp (oldest first)
        backup_files.sort(key=lambda x: x[0])

        current_count = len(backup_files)
        logger.info(f"VM {vmid}: Found {current_count} backups, limit is {copies_to_keep}")

        if current_count <= copies_to_keep:
            logger.debug(f"VM {vmid}: {current_count} backups <= {copies_to_keep} limit, nothing to delete")
            return

        # Delete oldest backups to stay within limit
        backups_to_delete = current_count - copies_to_keep
        deleted = 0

        for i in range(backups_to_delete):
            timestamp, entry = backup_files[i]

            # Delete the main backup file
            try:
                entry.unlink()
                logger.info(f"Deleted old backup file: {entry.name}")
                deleted += 1
            except OSError as e:
                logger.warning(f"Failed to delete {entry.name}: {e}")
                continue

            # Delete associated .notes file if exists
            notes_file = entry.parent / f"{entry.name}.notes"
            if notes_file.exists():
                try:
                    notes_file.unlink()
                    logger.debug(f"Deleted notes file: {notes_file.name}")
                except OSError:
                    pass

            # Delete associated .log file if exists
            base_name = (
                entry.name.replace(".vma.zst", "").replace(".vma.gz", "").replace(".vma.lzo", "").replace(".vma", "")
            )
            log_file = entry.parent / f"{base_name}.log"
            if log_file.exists():
                try:
                    log_file.unlink()
                    logger.debug(f"Deleted log file: {log_file.name}")
                except OSError:
                    pass

        if deleted > 0:
            logger.info(f"Retention enforcement: deleted {deleted} old backups for VM {vmid}, keeping {copies_to_keep}")

    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout enforcing retention for VM {vmid} from NFS")
    except Exception as e:
        logger.warning(f"Error enforcing retention for VM {vmid} from NFS: {e}")
    finally:
        # Unmount
        try:
            subprocess.run(["sudo", "-n", "umount", mount_point], capture_output=True, timeout=30)
        except Exception:
            try:
                subprocess.run(["sudo", "-n", "umount", "-l", mount_point], capture_output=True, timeout=10)
            except Exception:
                pass

        # Remove temp mount point
        try:
            import os

            os.rmdir(mount_point)
        except Exception:
            pass


def _enforce_copies_limit_hyperv_smb(
    repository: dict[str, Any],
    hypervisor_name: str,
    vm_name: str,
    copies_to_keep: int,
) -> None:
    """Enforce backup copies limit for a Hyper-V VM on SMB share.

    Hyper-V backups use a VM-centric folder structure:
    {repo_path}/Hypervisors/{hypervisor_name}/{vm_name}/{timestamp}/{vm_name}/

    This function lists timestamp folders, sorts them by timestamp,
    and recursively deletes the oldest folders to stay within the limit.
    """
    import re

    from backer.server.repositories import SMBBrowser

    server = repository.get("server", "")
    share = repository.get("share", "")
    subdir = repository.get("path", "").strip("/")
    username = repository.get("username")
    password = repository.get("password")
    domain = repository.get("domain")

    if not server or not share:
        return

    # Build path to VM's backup directory
    # Structure: Hypervisors/{hv_name}/{vm_name}/
    # (backup_vm creates: {backup_path}/{vm_name}/{timestamp}/)
    if subdir:
        vm_path = f"{subdir}/Hypervisors/{hypervisor_name}/{vm_name}"
    else:
        vm_path = f"Hypervisors/{hypervisor_name}/{vm_name}"

    try:
        logger.info(f"Checking retention for Hyper-V VM {vm_name} at //{server}/{share}/{vm_path}")

        # List timestamp folders in the VM directory using SMBBrowser
        success, entries = SMBBrowser.list_directory(server, share, vm_path, username, password, domain)
        if not success:
            error_msg = entries if isinstance(entries, str) else "Unknown error"
            logger.warning(f"Could not list Hyper-V backups in {vm_path}: {error_msg}")
            return

        # Find timestamp folders (format: YYYYMMDD_HHMMSS)
        timestamp_pattern = re.compile(r"^(\d{8}_\d{6})$")
        backup_folders: list[tuple[str, str]] = []  # (timestamp, folder_name)

        for entry in entries:
            if entry.name in [".", ".."]:
                continue
            if entry.is_dir:
                match = timestamp_pattern.match(entry.name)
                if match:
                    backup_folders.append((match.group(1), entry.name))

        # Sort by timestamp (oldest first)
        backup_folders.sort(key=lambda x: x[0])

        current_count = len(backup_folders)
        logger.info(f"VM {vm_name}: Found {current_count} Hyper-V backups, limit is {copies_to_keep}")

        if current_count <= copies_to_keep:
            logger.debug(f"VM {vm_name}: {current_count} backups <= {copies_to_keep} limit, nothing to delete")
            return

        # Delete oldest backup folders to stay within limit
        folders_to_delete = current_count - copies_to_keep
        deleted_count = 0

        # Build smbclient auth
        auth_opts = []
        if username:
            if password:
                auth_opts = [
                    "-U",
                    f"{domain}\\{username}%{password}" if domain else f"{username}%{password}",
                ]
            else:
                auth_opts = ["-U", f"{domain}\\{username}" if domain else username]
        else:
            auth_opts = ["-N"]

        for i in range(folders_to_delete):
            timestamp, folder_name = backup_folders[i]
            folder_path = f"{vm_path}/{folder_name}"

            # Recursively delete the timestamp folder and all contents
            deleted = _delete_smb_folder_recursive(server, share, folder_path, auth_opts, username, password, domain)
            if deleted:
                logger.info(f"Deleted old Hyper-V backup folder: {folder_name}")
                deleted_count += 1
            else:
                logger.warning(f"Failed to delete Hyper-V backup folder: {folder_name}")

        if deleted_count > 0:
            logger.info(
                f"Retention enforcement: deleted {deleted_count} old Hyper-V backups "
                f"for VM {vm_name}, keeping {copies_to_keep}"
            )

    except Exception as e:
        logger.warning(f"Error enforcing Hyper-V retention for VM {vm_name}: {e}")


def _delete_smb_folder_recursive(
    server: str,
    share: str,
    folder_path: str,
    auth_opts: list[str],
    username: str | None,
    password: str | None,
    domain: str | None,
) -> bool:
    """Recursively delete an SMB folder and all its contents.

    Returns True if successful, False otherwise.
    """
    import subprocess

    from backer.server.repositories import SMBBrowser

    try:
        # List contents of the folder
        success, entries = SMBBrowser.list_directory(server, share, folder_path, username, password, domain)
        if not success:
            error_msg = entries if isinstance(entries, str) else "Unknown error"
            logger.warning(f"[RETENTION] Cannot list folder {folder_path}: {error_msg}")
            return False

        logger.debug(f"[RETENTION] Found {len(entries)} entries in {folder_path}")

        # Delete contents first (files and subdirectories)
        for entry in entries:
            if entry.name in [".", ".."]:
                continue

            entry_path = f"{folder_path}/{entry.name}"

            if entry.is_dir:
                # Recursively delete subdirectory
                logger.debug(f"[RETENTION] Recursing into subdirectory: {entry_path}")
                sub_deleted = _delete_smb_folder_recursive(
                    server, share, entry_path, auth_opts, username, password, domain
                )
                if not sub_deleted:
                    logger.warning(f"[RETENTION] Failed to delete subdirectory: {entry_path}")
            else:
                # Delete file - use cd to parent dir then del filename to handle spaces
                logger.debug(f"[RETENTION] Deleting file: {entry_path}")
                del_cmd = [
                    "smbclient",
                    f"//{server}/{share}",
                    *auth_opts,
                    "-c",
                    f'cd "{folder_path}"; del "{entry.name}"',
                ]
                result = subprocess.run(del_cmd, capture_output=True, text=True, timeout=30)
                if result.returncode != 0:
                    logger.warning(f"[RETENTION] Failed to delete file {entry_path}: {result.stderr}")

        # Now delete the empty folder - cd to parent and rmdir the folder name
        # This handles paths with spaces better than trying to rmdir the full path
        parent_path = "/".join(folder_path.strip("/").split("/")[:-1])
        folder_name = folder_path.strip("/").split("/")[-1]
        logger.debug(f"[RETENTION] Removing directory: {folder_path} (parent={parent_path}, name={folder_name})")
        if parent_path:
            rmdir_cmd = [
                "smbclient",
                f"//{server}/{share}",
                *auth_opts,
                "-c",
                f'cd "{parent_path}"; rmdir "{folder_name}"',
            ]
        else:
            # Root level directory
            rmdir_cmd = [
                "smbclient",
                f"//{server}/{share}",
                *auth_opts,
                "-c",
                f'rmdir "{folder_name}"',
            ]
        result = subprocess.run(rmdir_cmd, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            stderr = result.stderr.strip()
            # Check if directory is not empty
            if "NT_STATUS_DIRECTORY_NOT_EMPTY" in stderr:
                logger.warning(f"[RETENTION] Directory not empty after deletion attempt: {folder_path}")
                # Re-list to see what's left
                success2, entries2 = SMBBrowser.list_directory(server, share, folder_path, username, password, domain)
                if success2:
                    remaining = [e.name for e in entries2 if e.name not in [".", ".."]]
                    logger.warning(f"[RETENTION] Remaining items: {remaining[:10]}")
            else:
                logger.warning(f"[RETENTION] rmdir failed for {folder_path}: {stderr}")
            return False

        logger.debug(f"[RETENTION] Successfully deleted: {folder_path}")
        return True

    except Exception as e:
        logger.warning(f"[RETENTION] Error deleting SMB folder {folder_path}: {e}")
        return False


def create_app(data_dir: Path | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    global _storage, _scheduler

    if data_dir is None:
        data_dir = Path.home() / ".local" / "share" / "backer"

    _storage = Storage(data_dir / "backer.db")

    # Initialize timezone module with storage reference
    tz.init_timezone(_storage)
    logger.info(f"Timezone configured: {tz.get_timezone()}")

    # Initialize scheduler
    _scheduler = BackupScheduler(
        storage=_storage,
        job_trigger_callback=trigger_job_internal,
        hypervisor_job_trigger_callback=trigger_hypervisor_job_internal,
        check_interval=60,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        logger.info("Starting Backer server...")

        if _scheduler:
            _scheduler.start()
        yield
        # Shutdown
        logger.info("Shutting down Backer server...")
        if _scheduler:
            _scheduler.stop()

    app = FastAPI(
        title="Backer Server",
        description="Centralized backup management server",
        version=__version__,
        lifespan=lifespan,
    )

    # CORS configuration
    # Note: allow_credentials=True with allow_origins=["*"] is insecure
    # For local deployments, we allow all origins but without credentials
    # If you need cross-origin requests with credentials, specify exact origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,  # Credentials require specific origins, not wildcard
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Global exception handler for malformed JSON requests
    @app.exception_handler(json.JSONDecodeError)
    async def json_decode_error_handler(request: Request, exc: json.JSONDecodeError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Invalid JSON in request body: {exc.msg}"},
        )

    # Health check
    @app.get("/health")
    def health_check() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    # ============ Background Tasks ============

    @app.get("/api/v1/tasks")
    def list_tasks(active_only: bool = False) -> list[dict[str, Any]]:
        """List background tasks.

        Args:
            active_only: If true, only return pending/running tasks
        """
        task_manager = get_task_manager()
        if active_only:
            tasks = task_manager.get_active_tasks()
        else:
            tasks = task_manager.get_recent_tasks(limit=20)
        return [t.to_dict() for t in tasks]

    @app.get("/api/v1/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        """Get status of a specific task."""
        task_manager = get_task_manager()
        task = task_manager.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return task.to_dict()

    # ============ Client Management ============

    @app.post("/api/v1/clients/register", response_model=ClientRegisterResponse)
    def register_client(
        request: ClientRegisterRequest,
        req: Request,
        credentials: HTTPBasicCredentials | None = Depends(security),
        storage: Storage = Depends(get_storage),
    ) -> ClientRegisterResponse:
        """Register a new client or rotate an existing client's credentials."""
        # Check if client with this hostname already exists
        existing_client = storage.get_client_by_hostname(request.hostname)

        if existing_client and credentials:
            # Re-registration is allowed only to the existing client. A
            # hostname is not a credential and must not permit a takeover.
            secret_hash = storage.get_client_secret_hash(existing_client.id)
            provided_hash = hashlib.sha256((credentials.password if credentials else "").encode()).hexdigest()
            if credentials.username == existing_client.id and secrets.compare_digest(secret_hash or "", provided_hash):
                client_secret = secrets.token_urlsafe(32)
                secret_hash = hashlib.sha256(client_secret.encode()).hexdigest()
                storage.update_client_secret(existing_client.id, secret_hash)

                storage.update_client_status(
                    existing_client.id,
                    ClientStatus.ONLINE,
                    ip_address=req.client.host if req.client else None,
                )

                logger.info(f"Re-registered existing client: {existing_client.id} ({request.hostname})")
                return ClientRegisterResponse(
                    client_id=existing_client.id,
                    client_secret=client_secret,
                    server_version=__version__,
                )
        elif existing_client:
            token_hash = hash_enrollment_code(request.enrollment_token)
            if not secrets.compare_digest(
                storage.get_setting("agent_enrollment_token_hash", ""), token_hash
            ) or enrollment_code_expired(storage.get_setting("agent_enrollment_token_expires")):
                raise HTTPException(status_code=401, detail="Existing client credentials required")

        # New clients need an admin-issued, single-use enrollment key. Duplicate
        # hostnames are display labels, not identities, so a valid token creates
        # a separate record instead of replacing the existing client.
        if not storage.consume_setting("agent_enrollment_token_hash", hash_enrollment_code(request.enrollment_token)):
            raise HTTPException(status_code=403, detail="Valid enrollment key required")
        if enrollment_code_expired(storage.get_setting("agent_enrollment_token_expires")):
            raise HTTPException(status_code=403, detail="Enrollment key expired")

        client_id = str(uuid4())[:8]
        client_secret = secrets.token_urlsafe(32)
        secret_hash = hashlib.sha256(client_secret.encode()).hexdigest()

        client = Client(
            id=client_id,
            name=request.hostname,
            hostname=request.hostname,
            ip_address=req.client.host if req.client else None,
            status=ClientStatus.ONLINE,
            last_seen=tz.get_now(),
            version=request.version,
            os_info=request.os_info,
            tags=request.tags,
        )

        storage.add_client(client, secret_hash)
        logger.info(f"Registered new client: {client_id} ({request.hostname})")

        return ClientRegisterResponse(
            client_id=client_id,
            client_secret=client_secret,
            server_version=__version__,
        )

    @app.post("/api/v1/clients/token")
    def get_agent_token(
        credentials: HTTPBasicCredentials | None = Depends(security),
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Generate a JWT token for an agent using client credentials.

        Authentication: HTTP Basic (client_id:client_secret)
        Returns: JWT token valid for 24 hours
        """
        if not credentials:
            raise HTTPException(
                status_code=401,
                detail="Client credentials required",
                headers={"WWW-Authenticate": "Basic"},
            )

        # Verify client exists and credentials are valid
        client = storage.get_client(credentials.username)
        if not client:
            raise HTTPException(status_code=401, detail="Invalid client ID")

        # Verify secret
        secret_hash = storage.get_client_secret_hash(credentials.username)
        provided_hash = hashlib.sha256(credentials.password.encode()).hexdigest()

        if not secrets.compare_digest(secret_hash or "", provided_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        # Generate JWT token
        try:
            token = generate_agent_token(
                client_id=credentials.username,
                additional_claims={
                    "client_name": client.name,
                    "client_hostname": client.hostname,
                },
            )

            logger.info(f"Generated token for client: {credentials.username} ({client.name})")

            return {
                "token": token,
                "token_type": "Bearer",
                "expires_in": 86400,  # 24 hours in seconds
                "client_id": credentials.username,
            }
        except Exception as e:
            logger.error(f"Failed to generate token: {e}")
            raise HTTPException(status_code=500, detail="Token generation failed")

    @app.get("/api/v1/clients", response_model=list[Client])
    def list_clients(storage: Storage = Depends(get_storage)) -> list[Client]:
        """List all registered clients."""
        return storage.list_clients()

    @app.get("/api/v1/clients/{client_id}", response_model=Client)
    def get_client(client_id: str, storage: Storage = Depends(get_storage)) -> Client:
        """Get a specific client by ID."""
        client = storage.get_client(client_id)
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")
        return client

    @app.delete("/api/v1/clients/{client_id}")
    def delete_client(
        client_id: str,
        delete_metadata: bool = True,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Remove a client and optionally clean up repository metadata.

        The client is removed from the database immediately.
        Metadata cleanup from repositories runs in the background.

        Args:
            client_id: The client ID to delete
            delete_metadata: If True (default), also delete agent metadata from all repositories
        """
        # Get client info before deleting (for hostname matching)
        client = storage.get_client(client_id)
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")

        client_name = client.name or client_id

        # Find all jobs associated with this agent
        all_jobs = storage.list_jobs()
        agent_jobs = [job for job in all_jobs if job.get("client_id") == client_id]

        # Delete client from database immediately
        if not storage.delete_client(client_id):
            raise HTTPException(status_code=404, detail="Client not found")

        # Delete all jobs associated with this agent
        jobs_deleted = 0
        for job in agent_jobs:
            job_name = job.get("name")
            if job_name and storage.delete_job(job_name):
                jobs_deleted += 1
                logger.info(f"[DELETE AGENT] Deleted associated job: {job_name}")

        result: dict[str, Any] = {"status": "deleted", "jobs_deleted": jobs_deleted}

        # Schedule metadata cleanup as a background task
        if delete_metadata:
            # Capture repository info for the background task
            repos = storage.list_repositories()
            repo_info = []
            for repo in repos:
                repo_id = repo.get("id")
                repo_info.append(
                    {
                        "repo_id": repo_id,
                        "repo_type": repo.get("repo_type", "smb"),
                        "server": repo.get("server", ""),
                        "share": repo.get("share", ""),
                        "path": repo.get("path", ""),
                        "username": repo.get("username"),
                        "password": storage.get_storage_password(repo_id) if repo_id else None,
                        "domain": repo.get("domain"),
                    }
                )

            def cleanup_agent_metadata(task: Task) -> dict[str, Any]:
                """Background task to clean up agent metadata from repositories."""
                from backer.server.repositories import smb_delete_file

                metadata_deleted = 0
                metadata_errors = []
                total = len(repo_info)

                for i, repo in enumerate(repo_info):
                    task.progress = int((i / total) * 100) if total > 0 else 0
                    task.message = f"Cleaning up repository {i + 1} of {total}..."

                    # Build path to agent metadata file
                    subpath = repo["path"]
                    metadata_base = f"{subpath}/.backer" if subpath else ".backer"
                    agent_path = f"{metadata_base}/agents/{client_id}.json"

                    try:
                        if repo["repo_type"] == "smb":
                            success, msg = smb_delete_file(
                                repo["server"],
                                repo["share"],
                                agent_path,
                                repo["username"],
                                repo["password"],
                                repo["domain"],
                            )
                            if success:
                                metadata_deleted += 1
                                logger.info(
                                    f"[DELETE AGENT] Deleted metadata from "
                                    f"{repo['server']}/{repo['share']}: {agent_path}"
                                )
                            elif "already deleted" not in msg.lower() and "no such file" not in msg.lower():
                                metadata_errors.append(f"{repo['server']}/{repo['share']}: {msg}")
                    except Exception as e:
                        metadata_errors.append(f"{repo['server']}/{repo['share']}: {str(e)}")
                        logger.warning(f"[DELETE AGENT] Error: {e}")

                return {"deleted": metadata_deleted, "errors": metadata_errors}

            task_manager = get_task_manager()
            task = task_manager.submit_with_func(
                task_type="delete_agent_metadata",
                description=f"Cleaning up metadata for agent '{client_name}'",
                func=cleanup_agent_metadata,
            )
            result["cleanup_task_id"] = task.id
            result["message"] = "Agent deleted. Metadata cleanup running in background."

        return result

    @app.post("/api/v1/clients/{client_id}/reset-credentials")
    def reset_client_credentials(client_id: str, storage: Storage = Depends(get_storage)) -> dict[str, Any]:
        """Reset credentials for an existing agent.

        Generates a new client_secret for the agent. The agent will need to
        re-register using this new secret. Returns a one-time registration
        token that can be used to re-register the agent.

        This is useful when an agent's credentials become invalid (e.g., after
        a reinstall where the old config was preserved but mismatched).
        """
        client = storage.get_client(client_id)
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")

        # Generate new credentials
        client_secret = secrets.token_urlsafe(32)
        secret_hash = hashlib.sha256(client_secret.encode()).hexdigest()
        storage.update_client_secret(client_id, secret_hash)

        logger.info(f"Reset credentials for client: {client_id} ({client.hostname})")

        return {
            "client_id": client_id,
            "client_secret": client_secret,
            "hostname": client.hostname,
            "message": "Credentials reset. Use the command below to reconfigure the agent.",
        }

    @app.post("/api/v1/commands/clear")
    def clear_pending_commands(
        storage: Storage = Depends(get_storage),
        client_id: str | None = None,
    ) -> dict[str, Any]:
        """Clear all pending commands from the queue.

        Optionally filter by client_id to only clear commands for a specific agent.
        This is useful for clearing stuck restore/backup commands.
        """
        count = storage.clear_pending_commands(client_id)
        return {
            "status": "ok",
            "cleared": count,
            "client_id": client_id,
        }

    @app.post("/api/v1/clients/heartbeat")
    async def client_heartbeat(
        heartbeat: ClientHeartbeat,
        req: Request,
        client: Client = Depends(verify_client),
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Receive heartbeat from a client.

        Uses long-polling: if no commands are pending, waits up to 25 seconds
        for a command to arrive before returning. This gives near-instant
        command delivery for interactive operations like file browsing.
        """
        import asyncio

        storage.update_client_status(client.id, ClientStatus.ONLINE, ip_address=req.client.host if req.client else None)

        # Check for pending commands immediately
        commands = storage.get_pending_commands(client.id)
        if commands:
            logger.info(f"[HEARTBEAT] Agent '{client.id}' receiving {len(commands)} command(s)")
            return {"status": "ok", "commands": _refresh_proxy_capabilities(commands, client.id)}

        # Long-polling: wait up to 25 seconds for a command to arrive
        # Check every 0.5 seconds for new commands
        for _ in range(50):  # 50 * 0.5s = 25 seconds max wait
            await asyncio.sleep(0.5)
            commands = storage.get_pending_commands(client.id)
            if commands:
                logger.info(f"[HEARTBEAT] Agent '{client.id}' receiving {len(commands)} command(s) (long-poll)")
                return {"status": "ok", "commands": _refresh_proxy_capabilities(commands, client.id)}

        # No commands after timeout - return empty
        return {"status": "ok", "commands": []}

    # ============ Job Management ============

    @app.post("/api/v1/jobs", response_model=JobResponse)
    def create_job(job: JobCreate, storage: Storage = Depends(get_storage)) -> JobResponse:
        """Create a new backup job."""
        # Validate job name for security (path traversal, special chars)
        validate_name(job.name, "Job name")
        if job.name.lower().startswith("restore:"):
            raise HTTPException(status_code=400, detail="Job name cannot start with 'restore:'")

        if storage.get_job(job.name):
            raise HTTPException(status_code=409, detail="Job already exists")

        config = job.model_dump()
        _validate_job_config(config, storage)
        storage.save_job(job.name, config)

        return JobResponse(
            name=job.name,
            source_path=job.source_path,
            destination_path=job.destination_path,
            client_id=job.client_id,
            enabled=True,
            schedule_cron=job.schedule_cron,
            last_run=None,
            last_status=None,
            next_run=None,
        )

    @app.get("/api/v1/jobs", response_model=list[JobResponse])
    def list_jobs(storage: Storage = Depends(get_storage)) -> list[JobResponse]:
        """List all backup jobs."""
        jobs = storage.list_jobs()
        responses = []
        for job in jobs:
            latest = storage.get_latest_run(job["name"])

            # Get next run from scheduler
            next_run = None
            schedule = job.get("schedule_cron")
            if schedule and job.get("enabled", True) and _scheduler:
                next_run = _scheduler.get_next_run(schedule)

            responses.append(
                JobResponse(
                    name=job["name"],
                    source_path=job.get("source_path", ""),
                    destination_path=job.get("destination_path", ""),
                    client_id=job.get("client_id"),
                    enabled=job.get("enabled", True),
                    schedule_cron=job.get("schedule_cron"),
                    last_run=datetime.fromisoformat(latest["started_at"]) if latest else None,
                    last_status=latest["status"] if latest else None,
                    next_run=next_run,
                )
            )
        return responses

    @app.get("/api/v1/jobs/{job_name}")
    def get_job(job_name: str, storage: Storage = Depends(get_storage)) -> dict[str, Any]:
        """Get a specific job."""
        job = storage.get_job(job_name)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return {"name": job_name, **_redact_secrets(job)}

    @app.put("/api/v1/jobs/{job_name}")
    async def update_job(
        job_name: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Update an existing job."""
        existing = storage.get_job(job_name)
        if not existing:
            raise HTTPException(status_code=404, detail="Job not found")

        data = await request.json()
        if {"backend", "backend_type", "backend_options"} & data.keys():
            raise HTTPException(status_code=422, detail="Backup engine fields are not supported")
        data = _restore_redacted_secrets(data, existing)

        # Merge retention if both exist and incoming is not empty
        if "retention" in data:
            if data["retention"] and "retention" in existing:
                merged_retention = {**existing.get("retention", {}), **data["retention"]}
                data["retention"] = merged_retention

        # Merge updates with existing config
        updated_config = {**existing, **data}
        _validate_job_config(updated_config, storage)
        storage.save_job(job_name, updated_config)

        return {"name": job_name, "status": "updated", **_redact_secrets(updated_config)}

    @app.patch("/api/v1/jobs/{job_name}/toggle")
    def toggle_job(job_name: str, storage: Storage = Depends(get_storage)) -> dict[str, Any]:
        """Toggle job enabled/disabled status."""
        existing = storage.get_job(job_name)
        if not existing:
            raise HTTPException(status_code=404, detail="Job not found")

        existing["enabled"] = not existing.get("enabled", False)
        storage.save_job(job_name, existing)

        return {"name": job_name, "enabled": existing["enabled"]}

    @app.delete("/api/v1/jobs/{job_name}")
    def delete_job(
        job_name: str,
        delete_snapshots: bool = False,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Delete a job and its repository metadata.

        The job is removed from the database immediately.
        Metadata cleanup from repositories runs in the background.

        Args:
            job_name: Name of the job to delete
            delete_snapshots: If True, also delete associated snapshot metadata
        """
        # Get job config before deleting (to find repository)
        job = storage.get_job(job_name)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        logger.info(f"[DELETE JOB] Deleting job '{job_name}', delete_snapshots={delete_snapshots}")

        # Capture info needed for background cleanup
        repo_id = job.get("repository_id")
        repo_info = None

        if repo_id:
            repo = storage.get_repository(repo_id)
            if repo:
                repo_info = {
                    "repo_type": repo.get("repo_type", "smb"),
                    "server": repo.get("server", ""),
                    "share": repo.get("share", ""),
                    "path": repo.get("path", ""),
                    "username": repo.get("username"),
                    "password": storage.get_storage_password(repo_id),
                    "domain": repo.get("domain"),
                    "mount_point": repo.get("mount_point"),
                }

        # Delete job from database immediately
        if not storage.delete_job(job_name):
            raise HTTPException(status_code=404, detail="Job not found")

        result: dict[str, Any] = {"status": "deleted"}

        # Schedule metadata cleanup as a background task if we have a repository
        if repo_info:
            # Get the job subfolder name for potential full deletion
            job_subfolder = _get_job_subfolder(job_name)

            def cleanup_job_metadata(task: Task) -> dict[str, Any]:
                """Background task to clean up job metadata from repository.

                If delete_snapshots is True, deletes the entire job subfolder
                (including all backup data). Otherwise, only deletes metadata.
                """
                import shutil
                from pathlib import Path as PathLib

                from backer.server.repositories import (
                    nfs_delete_directory,
                    smb_delete_directory,
                )

                job_deleted = False
                subfolder_deleted = False
                errors = []

                repo_type = repo_info["repo_type"]
                server = repo_info["server"]
                share = repo_info["share"]
                subpath = repo_info["path"]
                username = repo_info["username"]
                password = repo_info["password"]
                domain = repo_info["domain"]

                # Job subfolder path (where all backup data lives)
                # CRITICAL: Must match backup creation path structure: {base}/Agents/{job_subfolder}
                job_subfolder_path = f"{subpath}/Agents/{job_subfolder}" if subpath else f"Agents/{job_subfolder}"

                # Metadata path within the job subfolder
                metadata_job_path = f"{job_subfolder_path}/.backer/jobs/{job_name}"

                task.message = "Deleting job data..." if delete_snapshots else "Deleting job metadata..."
                task.progress = 10

                try:
                    if repo_type == "smb":
                        if delete_snapshots:
                            # Delete the entire job subfolder (all backup data + metadata)
                            task.message = f"Deleting job subfolder '{job_subfolder}'..."
                            success, msg = smb_delete_directory(
                                server, share, job_subfolder_path, username, password, domain
                            )
                            if success:
                                subfolder_deleted = True
                                job_deleted = True
                                logger.info(f"Deleted entire job subfolder from SMB: {job_subfolder_path}")
                            elif "no such file" not in msg.lower() and "not found" not in msg.lower():
                                errors.append(f"Job subfolder: {msg}")
                        else:
                            # Only delete job metadata, keep backup data
                            success, msg = smb_delete_directory(
                                server, share, metadata_job_path, username, password, domain
                            )
                            if success:
                                job_deleted = True
                                logger.info(f"Deleted job metadata from SMB: {metadata_job_path}")
                            elif "no such file" not in msg.lower() and "not found" not in msg.lower():
                                errors.append(f"Job metadata: {msg}")

                        task.progress = 90

                    elif repo_type == "local" or repo_info.get("mount_point"):
                        # Direct filesystem access (local or pre-mounted)
                        if repo_info.get("mount_point"):
                            base_path = PathLib(repo_info["mount_point"])
                        else:
                            base_path = PathLib(share or repo_info.get("path", ""))

                        if subpath:
                            base_path = base_path / subpath

                        # Job data is at: {base}/Agents/{job_subfolder}/
                        agents_job_path = base_path / "Agents" / job_subfolder

                        if delete_snapshots:
                            # Delete entire job subfolder (all backup data + metadata)
                            task.message = f"Deleting job subfolder '{job_subfolder}'..."
                            if agents_job_path.exists():
                                shutil.rmtree(agents_job_path)
                                subfolder_deleted = True
                                job_deleted = True
                                logger.info(f"[LOCAL DELETE] Deleted entire job subfolder: {agents_job_path}")
                            else:
                                logger.info(f"[LOCAL DELETE] Job subfolder does not exist: {agents_job_path}")
                                job_deleted = True  # Already gone
                        else:
                            # Only delete job metadata, keep backup data
                            job_metadata_path = agents_job_path / ".backer" / "jobs" / job_name
                            if job_metadata_path.exists():
                                shutil.rmtree(job_metadata_path)
                                job_deleted = True
                                logger.info(f"[LOCAL DELETE] Deleted job metadata: {job_metadata_path}")

                        task.progress = 90

                    # NFS - use mount_point if available, otherwise temp mount
                    elif repo_type == "nfs":
                        if repo_info.get("mount_point"):
                            # NFS already mounted - use filesystem directly
                            base_path = PathLib(repo_info["mount_point"])
                            if subpath:
                                base_path = base_path / subpath

                            # Job data is at: {base}/Agents/{job_subfolder}/
                            agents_job_path = base_path / "Agents" / job_subfolder

                            if delete_snapshots:
                                task.message = f"Deleting job subfolder '{job_subfolder}'..."
                                if agents_job_path.exists():
                                    shutil.rmtree(agents_job_path)
                                    subfolder_deleted = True
                                    job_deleted = True
                                    logger.info(f"[NFS DELETE] Deleted entire job subfolder: {agents_job_path}")
                                else:
                                    logger.info(f"[NFS DELETE] Job subfolder does not exist: {agents_job_path}")
                                    job_deleted = True
                            else:
                                job_metadata_path = agents_job_path / ".backer" / "jobs" / job_name
                                if job_metadata_path.exists():
                                    shutil.rmtree(job_metadata_path)
                                    job_deleted = True
                                    logger.info(f"[NFS DELETE] Deleted job metadata: {job_metadata_path}")

                            task.progress = 90
                        else:
                            # NFS not mounted - use nfs_delete_directory to temp mount
                            if delete_snapshots:
                                task.message = f"Deleting job subfolder '{job_subfolder}' from NFS..."
                                success, msg = nfs_delete_directory(server, share, job_subfolder_path)
                                if success:
                                    subfolder_deleted = True
                                    job_deleted = True
                                    logger.info(f"Deleted entire job subfolder from NFS: {job_subfolder_path}")
                                elif "no such file" not in msg.lower() and "not found" not in msg.lower():
                                    errors.append(f"Job subfolder: {msg}")
                            else:
                                # Only delete job metadata
                                success, msg = nfs_delete_directory(server, share, metadata_job_path)
                                if success:
                                    job_deleted = True
                                    logger.info(f"Deleted job metadata from NFS: {metadata_job_path}")
                                elif "no such file" not in msg.lower() and "not found" not in msg.lower():
                                    errors.append(f"Job metadata: {msg}")

                        task.progress = 90

                except Exception as e:
                    errors.append(str(e))
                    logger.exception(f"Error in job metadata cleanup: {e}")

                return {
                    "job_deleted": job_deleted,
                    "subfolder_deleted": subfolder_deleted,
                    "errors": errors,
                }

            task_manager = get_task_manager()
            task_desc = (
                f"Deleting all data for job '{job_name}'"
                if delete_snapshots
                else f"Cleaning up metadata for job '{job_name}'"
            )
            task = task_manager.submit(
                task_type="delete_job_data" if delete_snapshots else "delete_job_metadata",
                description=task_desc,
                func=cleanup_job_metadata,
            )
            result["cleanup_task_id"] = task.id
            result["message"] = (
                "Job deleted. Backup data cleanup running in background."
                if delete_snapshots
                else "Job deleted. Metadata cleanup running in background."
            )

        return result

    @app.post("/api/v1/jobs/{job_name}/run", response_model=JobRunResponse)
    def run_job(
        job_name: str,
        request: JobRunRequest | None = None,
        storage: Storage = Depends(get_storage),
    ) -> JobRunResponse:
        """Trigger a job to run."""
        # Default to non-dry-run if no request body provided
        dry_run = request.dry_run if request else False
        override_client_id = request.override_client_id if request else None
        update_job_agent = request.update_job_agent if request else False

        job = storage.get_job(job_name)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        # Determine which client to use (override takes precedence)
        client_id = override_client_id or job.get("client_id")
        if not client_id:
            raise HTTPException(
                status_code=400,
                detail="Job has no assigned agent. Assign an agent to run this job.",
            )

        # Check client exists
        client = storage.get_client(client_id)
        if not client:
            raise HTTPException(status_code=400, detail=f"Agent '{client_id}' not found")

        run_config = {**job, "client_id": client_id}
        _validate_job_config(run_config, storage)

        # If user wants to update the job with the new agent, do so
        if override_client_id and update_job_agent:
            job["client_id"] = override_client_id
            storage.save_job(job_name, job)
            logger.info(f"[JOB RUN] Updated job '{job_name}' to use agent '{override_client_id}'")

        # Check if job is already running (prevent duplicate runs)
        recent_runs = storage.get_job_runs(job_name, limit=5)
        for run in recent_runs:
            if run.get("status") in ("pending", "running"):
                raise HTTPException(
                    status_code=409, detail=f"Job '{job_name}' is already running (run_id: {run.get('run_id')})"
                )

        now = tz.get_now()
        run_id = now.strftime("%Y%m%d_%H%M%S_%f")
        started_at = now

        # Save the run record as "pending"
        storage.save_job_run(
            run_id=run_id,
            job_name=job_name,
            status="pending",
            started_at=started_at,
            client_id=client_id,
        )

        # Start progress tracking
        storage.start_job_progress(
            run_id=run_id,
            job_name=job_name,
            client_id=client_id,
        )

        # Build and queue the backup command with repository credentials
        command_payload = _build_backup_command_payload(
            job=job,
            job_name=job_name,
            run_id=run_id,
            dry_run=dry_run,
            storage=storage,
            client_id=client_id,
        )

        storage.queue_command(
            client_id=client_id,
            command_type="backup",
            payload=command_payload,
        )

        logger.info(f"[JOB RUN] Job '{job_name}' queued for agent '{client_id}' (run_id: {run_id})")

        return JobRunResponse(
            run_id=run_id,
            job_name=job_name,
            status="pending",
            started_at=started_at,
            message=f"Backup queued for agent '{client_id}'" + (" (dry run)" if dry_run else ""),
        )

    @app.get("/api/v1/jobs/{job_name}/runs")
    def get_job_runs(job_name: str, limit: int = 20, storage: Storage = Depends(get_storage)) -> list[dict[str, Any]]:
        """Get run history for a job."""
        if not storage.get_job(job_name):
            raise HTTPException(status_code=404, detail="Job not found")
        return storage.get_job_runs(job_name, limit)

    @app.get("/api/v1/runs")
    def get_all_runs(limit: int = 50, storage: Storage = Depends(get_storage)) -> list[dict[str, Any]]:
        """List recent runs for the History live-update view."""
        return storage.get_all_job_runs(limit=limit)

    @app.get("/api/v1/runs/{run_id}")
    def get_run_details(run_id: str, storage: Storage = Depends(get_storage)) -> dict[str, Any]:
        """Get details for a specific job run including logs and output."""
        run = storage.get_job_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        # Parse errors JSON if stored as string
        errors = run.get("errors", [])
        if isinstance(errors, str):
            try:
                errors = json.loads(errors)
            except json.JSONDecodeError:
                errors = [errors] if errors else []

        return {
            "run_id": run["run_id"],
            "job_name": run["job_name"],
            "status": run["status"],
            "client_id": run.get("client_id"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "bytes_transferred": run.get("bytes_transferred", 0),
            "files_transferred": run.get("files_transferred", 0),
            "errors": _redact_secrets(errors),
            "output": _redact_secrets(run.get("output", "")),
            "snapshot_id": run.get("snapshot_id"),
        }

    # ============ Command acknowledgement ============

    @app.post("/api/v1/commands/{command_id}/ack")
    def acknowledge_command(
        command_id: int,
        client: Client = Depends(verify_client),
        storage: Storage = Depends(get_storage),
    ) -> dict[str, str]:
        """Mark a command as received/executed by the client."""
        storage.mark_command_executed(command_id)
        return {"status": "acknowledged"}

    # ============ Client-reported results ============

    @app.post("/api/v1/results")
    def report_result(
        result: BackupResult,
        client: Client = Depends(verify_client),
        storage: Storage = Depends(get_storage),
    ) -> dict[str, str]:
        """Client reports backup result."""
        status = "success" if result.success else "failed"
        logger.info(f"[RESULT] Job '{result.job_name}' (run_id: {result.run_id}) completed with status: {status}")
        if result.errors:
            logger.warning(f"[RESULT] Job '{result.job_name}' errors: {result.errors}")
        if result.output:
            logger.debug(f"[RESULT] Job '{result.job_name}' output (first 500 chars): {result.output[:500]}")

        storage.save_job_run(
            run_id=result.run_id,
            job_name=result.job_name,
            status=status,
            started_at=result.started_at,
            finished_at=result.finished_at,
            client_id=result.client_id,
            bytes_transferred=result.bytes_transferred,
            files_transferred=result.files_transferred,
            errors=result.errors,
            output=result.output[:10000],  # Limit output size
            snapshot_id=result.snapshot_id,
        )
        # Mark progress as complete
        storage.finish_job_progress(result.run_id, status="completed" if result.success else "failed")
        return {"status": "recorded"}

    # ============ Progress Tracking ============

    @app.post("/api/v1/progress")
    async def report_progress(
        request: Request,
        client: Client = Depends(verify_client),
        storage: Storage = Depends(get_storage),
    ) -> dict[str, str]:
        """Client reports backup progress update."""
        data = await request.json()
        run_id = data.get("run_id")
        if not run_id:
            raise HTTPException(status_code=400, detail="run_id required")

        storage.update_job_progress(
            run_id=run_id,
            status=data.get("status"),
            progress_percent=data.get("progress_percent"),
            current_file=data.get("current_file"),
            bytes_processed=data.get("bytes_processed"),
            files_processed=data.get("files_processed"),
            total_bytes=data.get("total_bytes"),
            total_files=data.get("total_files"),
            message=data.get("message"),
        )
        return {"status": "updated"}

    @app.get("/api/v1/running")
    def get_running_jobs(storage: Storage = Depends(get_storage)) -> list[dict[str, Any]]:
        """Get all currently running backup jobs."""
        return storage.get_running_jobs()

    @app.get("/api/v1/progress/{run_id}")
    def get_progress(run_id: str, storage: Storage = Depends(get_storage)) -> dict[str, Any]:
        """Get progress for a specific job run."""
        progress = storage.get_job_progress(run_id)
        if not progress:
            raise HTTPException(status_code=404, detail="Run not found")
        return progress

    # ============ Agent Filesystem Browsing ============

    @app.post("/api/v1/agents/{client_id}/browse")
    async def browse_agent_filesystem(
        client_id: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Request filesystem listing from an agent.

        Body:
        - path: Directory path to browse (optional, defaults to root/home)
        """
        client = storage.get_client(client_id)
        if not client:
            raise HTTPException(status_code=404, detail="Agent not found")

        try:
            data = await request.json()
        except Exception:
            data = {}
        path = data.get("path", "")

        # Generate a unique request ID
        request_id = f"browse_{tz.get_now().strftime('%Y%m%d_%H%M%S_%f')}"

        # Queue the browse command for the agent
        storage.queue_command(
            client_id=client_id,
            command_type="browse_filesystem",
            payload={
                "request_id": request_id,
                "path": path,
            },
        )

        # Store pending browse request
        storage.save_browse_request(request_id, client_id, path)

        logger.info(f"[BROWSE] Queued browse for agent '{client_id}', path='{path}', id={request_id}")

        return {
            "request_id": request_id,
            "status": "pending",
            "client_id": client_id,
            "path": path,
        }

    @app.get("/api/v1/browse/{request_id}")
    def get_browse_results(
        request_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Get results of a filesystem browse request."""
        result = storage.get_browse_result(request_id)
        if not result:
            raise HTTPException(status_code=404, detail="Browse request not found")
        return result

    @app.post("/api/v1/browse/{request_id}/results")
    async def report_browse_results(
        request_id: str,
        request: Request,
        client: Client = Depends(verify_client),
        storage: Storage = Depends(get_storage),
    ) -> dict[str, str]:
        """Agent reports filesystem browse results."""
        data = await request.json()

        storage.save_browse_result(
            request_id=request_id,
            status="completed" if data.get("success", True) else "error",
            entries=data.get("entries", []),
            error=data.get("error"),
            path=data.get("path", ""),
        )

        logger.info(f"[BROWSE] Results received for request_id={request_id}, entries={len(data.get('entries', []))}")

        return {"status": "recorded"}

    # ============ Restore Operations ============

    @app.post("/api/v1/restore")
    async def trigger_restore(
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Trigger a restore operation.

        Required fields in request body:
        - job_name: Name of the job to restore from
        - client_id: Agent to run the restore on

        Optional fields:
        - destination_path: Where to restore files to (defaults to original source path)
        - run_id: Specific backup run to restore from (defaults to latest)
        - source_subfolder: Subfolder within backup to restore
        - snapshot: Specific snapshot ID to restore (defaults to latest)
        - dry_run: If true, don't actually restore
        """
        data = await request.json()

        job_name = data.get("job_name")
        client_id = data.get("client_id")

        if not job_name:
            raise HTTPException(status_code=400, detail="job_name required")
        if not client_id:
            raise HTTPException(status_code=400, detail="client_id required")

        # Get job config
        job = storage.get_job(job_name)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        # Default destination to original source path if not specified
        destination_path = data.get("destination_path") or job.get("source_path")

        # Verify client exists
        client = storage.get_client(client_id)
        if not client:
            raise HTTPException(status_code=400, detail=f"Agent '{client_id}' not found")

        _validate_job_config({**job, "client_id": client_id}, storage)

        # Get backup source path (this is the backup destination in the job)
        # Note: This is the base path from job config, we need to add job subfolder
        backup_source = job.get("destination_path")

        # For imported jobs, destination_path might be missing - look up from repository
        if not backup_source and job.get("repository_id"):
            repo = storage.get_repository(job["repository_id"])
            if repo:
                repo_type = repo.get("repo_type", "smb")
                if repo_type == "smb":
                    backup_source = f"//{repo['server']}/{repo['share']}"
                elif repo_type == "nfs":
                    backup_source = f"{repo['server']}:{repo['share']}"
                else:
                    backup_source = repo.get("share", "") or repo.get("path", "")
                if backup_source and repo.get("path"):
                    backup_source = f"{backup_source}/{repo['path']}"

        if not backup_source and not job.get("repository_id"):
            raise HTTPException(
                status_code=400,
                detail="Job is missing destination_path and no repository configured. "
                "Please re-import or reconfigure the job.",
            )

        # Append job-specific subfolder to backup source
        # This matches where _build_backup_command_payload puts the backup
        # Structure is: {repo_path}/Agents/{job_name}
        job_subfolder = _get_job_subfolder(job_name)
        backup_source = f"{backup_source.rstrip('/')}/Agents/{job_subfolder}"

        source_subfolder = data.get("source_subfolder", "")
        if data.get("clean_restore") and data.get("dry_run"):
            raise HTTPException(status_code=400, detail="clean_restore cannot be combined with dry_run")

        now = tz.get_now()
        restore_id = now.strftime("%Y%m%d_%H%M%S_%f")
        started_at = now

        # Save restore record
        storage.save_job_run(
            run_id=f"restore_{restore_id}",
            job_name=f"restore:{job_name}",
            status="pending",
            started_at=started_at,
            client_id=client_id,
        )

        # Start progress tracking
        storage.start_job_progress(
            run_id=f"restore_{restore_id}",
            job_name=f"restore:{job_name}",
            client_id=client_id,
        )

        # Queue restore command for the agent
        # Include SMB/NFS credentials for agents that need to mount the backup source
        # Check if the target client is an Android device
        # Android agents cannot mount SMB/NFS shares, so they must use proxy backend
        is_android_client = False
        os_info = getattr(client, "os_info", "") or ""
        is_android_client = os_info.lower().startswith("android")
        if is_android_client:
            logger.debug(f"[RESTORE] Detected Android client: {client_id} (os_info={os_info})")

        command_payload = {
            "job_name": job_name,
            "run_id": f"restore_{restore_id}",
            "source_path": backup_source,  # Restore FROM backup location (repository)
            "destination_path": destination_path,  # Restore TO this location
            "original_source_path": job.get("source_path"),
            "snapshot": data.get("snapshot"),
            "source_subfolder": source_subfolder,
            "clean_restore": data.get("clean_restore", False),  # Delete extra files at destination
            "dry_run": data.get("dry_run", False),
        }

        # Add repository credentials for SMB/NFS access
        repository_id = job.get("repository_id")
        if not repository_id:
            raise HTTPException(status_code=400, detail="A repository is required for restores")
        repo = storage.get_repository(repository_id)
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found")
        if repo:
            repo_type = repo.get("repo_type", "")
            repo_password = storage.get_repository_password(repository_id)
            if not repo_password:
                raise HTTPException(status_code=400, detail="Repository encryption password is required")
            command_payload["repository_options"] = {"repository_password": repo_password}
            storage_password = storage.get_storage_password(repository_id)

            if repo_type == "smb":
                # Include SMB connection info for agents that can mount shares.
                command_payload["smb_server"] = repo.get("server")
                command_payload["smb_share"] = repo.get("share")
                command_payload["smb_username"] = repo.get("username")
                command_payload["smb_domain"] = repo.get("domain")
                if storage_password:
                    command_payload["smb_password"] = storage_password

            elif repo_type == "nfs":
                # NFS info (no password needed typically)
                command_payload["nfs_server"] = repo.get("server")
                command_payload["nfs_export"] = repo.get("share")

            elif repo_type == "local":
                # For local repositories, use proxy backend for restore
                # The agent streams restore data FROM the server via HTTP/HTTPS
                public_url = storage.get_setting("public_url", "http://localhost:8420")

                # Determine proxy scheme based on public_url protocol
                if public_url.startswith("https://"):
                    proxy_scheme = "proxys"
                    host_part = public_url[8:]  # Remove "https://"
                else:
                    proxy_scheme = "proxy"
                    host_part = public_url[7:]  # Remove "http://"

                # Generate proxy URI with job subfolder
                # Structure: proxy://host/repo/{id}/Agents/{job}
                proxy_uri = f"{proxy_scheme}://{host_part}/repo/{repository_id}/Agents/{job_subfolder}"

                # Override source_path with proxy URI
                command_payload["source_path"] = proxy_uri

                command_payload["repository_options"]["proxy_capability"] = generate_proxy_capability(
                    client_id=client_id,
                    repo_id=repository_id,
                    job_name=job_name,
                    run_id=f"restore_{restore_id}",
                    subfolder=f"Agents/{job_subfolder}",
                    operation="restore",
                )

                logger.debug(f"[RESTORE] Using proxy for local repo: {proxy_uri}")

            elif repo_type == "s3":
                from backer.backends.s3 import kopia_s3_config

                s3 = {
                    **repo.get("config", {}).get("s3", {}),
                    **(storage.get_repository_provider_credentials(repository_id) or {}),
                }
                s3_config = kopia_s3_config(s3)
                command_payload["source_path"] = s3_config["repository"]
                command_payload["repository_options"]["s3"] = s3

        storage.queue_command(
            client_id=client_id,
            command_type="restore",
            payload=command_payload,
        )

        return {
            "restore_id": f"restore_{restore_id}",
            "job_name": job_name,
            "status": "pending",
            "started_at": started_at.isoformat(),
            "message": f"Restore queued for agent '{client_id}'",
        }

    @app.get("/api/v1/jobs/{job_name}/backups")
    def list_job_backups(
        job_name: str,
        limit: int = 20,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List available backups for a job (successful runs that can be restored from)."""
        if not storage.get_job(job_name):
            raise HTTPException(status_code=404, detail="Job not found")

        runs = storage.get_job_runs(job_name, limit=limit)
        # Filter to only successful backups (not restores)
        backups = [
            {
                "run_id": r["run_id"],
                "started_at": r.get("started_at"),
                "finished_at": r.get("finished_at"),
                "bytes_transferred": r.get("bytes_transferred", 0),
                "files_transferred": r.get("files_transferred", 0),
                "snapshot_id": r.get("snapshot_id"),
            }
            for r in runs
            if r.get("status") == "success" and not r.get("run_id", "").startswith("restore_")
        ]
        return backups

    # ============ Scheduler Status ============

    @app.get("/api/v1/scheduler/status")
    def get_scheduler_status() -> dict[str, Any]:
        """Get scheduler status and upcoming jobs."""
        if not _scheduler:
            return {"enabled": False, "message": "Scheduler not initialized"}

        return {
            "enabled": True,
            "jobs": _scheduler.get_job_status(),
        }

    # ============ Retention Policies ============

    @app.get("/api/v1/retention/presets")
    def get_retention_presets() -> dict[str, Any]:
        """Get available retention policy presets."""
        from backer.server.retention import RETENTION_PRESETS

        return {"presets": RETENTION_PRESETS}

    @app.post("/api/v1/jobs/{job_name}/retention")
    async def set_job_retention(
        job_name: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Set retention policy for a job."""
        job = storage.get_job(job_name)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        data = await request.json()
        job["retention"] = data
        storage.save_job(job_name, job)

        return {"status": "updated", "retention": data}

    @app.post("/api/v1/jobs/{job_name}/retention/apply")
    def apply_job_retention(
        job_name: str,
        dry_run: bool = False,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Apply retention policy to a job (clean up old runs)."""
        from backer.server.retention import RetentionManager

        manager = RetentionManager(storage)
        deleted = manager.apply_retention(job_name, dry_run=dry_run)

        return {
            "job_name": job_name,
            "dry_run": dry_run,
            "deleted_count": len(deleted),
            "deleted_runs": [{"run_id": r["run_id"], "started_at": r.get("started_at")} for r in deleted],
        }

    @app.post("/api/v1/retention/apply-all")
    def apply_all_retention(
        dry_run: bool = False,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Apply retention policies to all jobs."""
        from backer.server.retention import RetentionManager

        manager = RetentionManager(storage)
        results = manager.apply_all_retention(dry_run=dry_run)

        return {
            "dry_run": dry_run,
            "jobs_processed": len(results),
            "total_deleted": sum(len(runs) for runs in results.values()),
            "details": {
                job: [{"run_id": r["run_id"], "started_at": r.get("started_at")} for r in runs]
                for job, runs in results.items()
            },
        }

    # ============ Storage Repositories ============

    @app.post("/api/v1/repositories/discover")
    async def discover_shares_endpoint(
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Discover available shares on a server."""
        import asyncio

        from backer.server.repositories import (
            RepositoryType,
        )
        from backer.server.repositories import (
            discover_shares as do_discover,
        )

        data = await request.json()

        repo_type = data.get("type", "smb")
        server = data.get("server", "")
        username = data.get("username")
        password = data.get("password")
        domain = data.get("domain")

        if not server:
            raise HTTPException(status_code=400, detail="Server address required")

        try:
            rtype = RepositoryType(repo_type)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid type: {repo_type}")

        # Run blocking SMB operation in thread pool to not block event loop
        loop = asyncio.get_event_loop()
        success, result = await loop.run_in_executor(
            None, lambda: do_discover(rtype, server, username, password, domain)
        )

        if success:
            return {
                "success": True,
                "shares": [{"name": s.name, "type": s.share_type, "comment": s.comment} for s in result],
            }
        else:
            return {"success": False, "error": result}

    @app.post("/api/v1/repositories/browse")
    async def browse_share_endpoint(
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Browse a directory on a share."""
        import asyncio

        from backer.server.repositories import (
            RepositoryType,
            browse_directory,
        )

        data = await request.json()

        repo_type = data.get("type", "smb")
        server = data.get("server", "")
        share = data.get("share", "")
        path = data.get("path", "")
        username = data.get("username")
        password = data.get("password")
        domain = data.get("domain")

        if not server or not share:
            raise HTTPException(status_code=400, detail="Server and share required")

        try:
            rtype = RepositoryType(repo_type)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid type: {repo_type}")

        # Run blocking SMB operation in thread pool to not block event loop
        loop = asyncio.get_event_loop()
        success, result = await loop.run_in_executor(
            None, lambda: browse_directory(rtype, server, share, path, username, password, domain)
        )

        if success:
            return {
                "success": True,
                "path": path,
                "entries": [{"name": e.name, "is_dir": e.is_dir, "size": e.size} for e in result],
            }
        else:
            return {"success": False, "error": result}

    @app.get("/api/v1/repositories")
    def list_repositories(storage: Storage = Depends(get_storage)) -> list[dict[str, Any]]:
        """List all storage repositories."""
        return storage.list_repositories()

    @app.post("/api/v1/repositories")
    async def create_repository(
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Create a new storage repository.

        Automatically triggers test and scan operations after creation.
        """
        from backer.server.secrets import get_secrets_manager

        data = await request.json()
        if {"backend", "backend_type", "backend_options"} & data.keys():
            raise HTTPException(status_code=422, detail="Backup engine fields are not supported")

        name = data.get("name", "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Name required")
        # Validate repository name for security
        validate_name(name, "Repository name")

        if storage.get_repository_by_name(name):
            raise HTTPException(status_code=409, detail="Repository name already exists")

        repo_type = data.get("type", "smb")
        if repo_type not in {"smb", "nfs", "local", "s3"}:
            raise HTTPException(status_code=400, detail="Unsupported repository type")
        repository_password = _repository_password_or_error(data.get("repository_password"))

        repo_id = str(uuid4())[:8]
        config: dict[str, Any] | None = None
        provider_credentials_encrypted = None
        if repo_type == "s3":
            from backer.backends.s3 import S3ConfigError, kopia_s3_config

            try:
                s3 = kopia_s3_config(data.get("s3") or {})
            except S3ConfigError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            data["share"], data["path"] = s3["public_config"]["bucket"], s3["public_config"]["prefix"]
            config = {"s3": s3["public_config"]}
            provider_credentials_encrypted = get_secrets_manager(storage.db_path.parent).encrypt(
                json.dumps(
                    {
                        "access_key_id": s3["environment"]["AWS_ACCESS_KEY_ID"],
                        "secret_access_key": s3["environment"]["AWS_SECRET_ACCESS_KEY"],
                    }
                )
            )

        # Validate local repository paths
        if repo_type == "local":
            import getpass
            import stat

            local_path = data.get("share", "").strip()
            if not local_path:
                raise HTTPException(status_code=400, detail="Local path required")

            # Normalize and validate path (cross-platform)
            try:
                path_obj = Path(local_path).resolve().absolute()

                # Log current process permissions for debugging
                current_user = getpass.getuser()
                current_uid = os.getuid() if hasattr(os, "getuid") else "N/A"
                logger.info(f"[CREATE REPO] Running as user: {current_user} (UID: {current_uid})")
                logger.info(f"[CREATE REPO] Validating local path: {path_obj}")

                # Check if path exists or can be created
                if not path_obj.exists():
                    logger.info(f"[CREATE REPO] Path doesn't exist, attempting to create: {path_obj}")
                    try:
                        path_obj.mkdir(parents=True, exist_ok=True)
                        logger.info(f"[CREATE REPO] Successfully created directory: {path_obj}")
                    except Exception as mkdir_err:
                        logger.error(f"[CREATE REPO] Failed to create directory: {mkdir_err}")
                        raise
                else:
                    logger.info(f"[CREATE REPO] Path already exists: {path_obj}")
                    try:
                        st = path_obj.stat()
                        mode = stat.filemode(st.st_mode)
                        owner_uid = st.st_uid
                        logger.info(f"[CREATE REPO] Directory permissions: {mode} owner UID: {owner_uid}")
                    except Exception as stat_err:
                        logger.warning(f"[CREATE REPO] Could not stat directory: {stat_err}")

                # Verify it's readable and writable
                readable = os.access(path_obj, os.R_OK)
                writable = os.access(path_obj, os.W_OK)
                logger.info(f"[CREATE REPO] Permission check - readable: {readable}, writable: {writable}")

                if not readable or not writable:
                    logger.error(f"[CREATE REPO] Insufficient permissions for {path_obj}")
                    raise HTTPException(
                        status_code=400,
                        detail=f"Path must be readable and writable (readable={readable}, writable={writable})",
                    )

                # Update share with absolute normalized path (cross-platform compatible)
                data["share"] = str(path_obj)
                logger.info(f"[CREATE REPO] Successfully validated local path: {path_obj}")
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"[CREATE REPO] Failed to validate local path '{local_path}': {e}", exc_info=True)
                raise HTTPException(status_code=400, detail=f"Invalid local path: {str(e)}")

        storage_password = data.get("storage_password") or (data.get("password") if repo_type == "smb" else None)
        storage_password_encrypted = repository_password_encrypted = None
        secrets_manager = get_secrets_manager(storage.db_path.parent)
        storage_password_encrypted = secrets_manager.encrypt(storage_password) if storage_password else None
        repository_password_encrypted = secrets_manager.encrypt(repository_password)

        storage.add_repository(
            repo_id=repo_id,
            name=name,
            repo_type=repo_type,
            server=data.get("server"),
            share=data.get("share"),
            path=data.get("path", ""),
            username=data.get("username"),
            storage_password_encrypted=storage_password_encrypted,
            repository_password_encrypted=repository_password_encrypted,
            domain=data.get("domain"),
            config=config,
            provider_credentials_encrypted=provider_credentials_encrypted,
        )

        # Initialize kopia repository for LOCAL repos
        if repo_type == "local":
            local_path = data.get("share")
            kopia_password = _repository_password_or_error(repository_password)
            try:
                kopia = ServerKopia(local_path, kopia_password)
                if kopia.ensure_repo():
                    logger.info(f"[CREATE REPO] Kopia repository initialized at {local_path}")
                else:
                    logger.warning(f"[CREATE REPO] Failed to initialize kopia repository at {local_path}")
            except Exception as kopia_err:
                logger.warning(f"[CREATE REPO] Kopia initialization error: {kopia_err}")
                # Don't fail - kopia will be initialized on first backup

        # Auto-trigger test and scan for better UX
        test_task_id = None
        scan_task_id = None

        try:
            # Start test task
            test_result = test_repository(repo_id, storage)
            test_task_id = test_result.get("task_id")

            if repo_type != "s3":
                scan_result = scan_repository(repo_id, storage)
                scan_task_id = scan_result.get("task_id")
        except Exception as e:
            logger.warning(f"Failed to auto-trigger test/scan for repository {repo_id}: {e}")

        return {
            "id": repo_id,
            "name": name,
            "status": "created",
            "test_task_id": test_task_id,
            "scan_task_id": scan_task_id,
        }

    @app.delete("/api/v1/repositories/{repo_id}")
    def delete_repository(repo_id: str, storage: Storage = Depends(get_storage)) -> dict[str, Any]:
        """Delete a repository and all jobs that use it."""
        # First, find and delete all jobs that use this repository
        jobs = storage.list_jobs()
        deleted_jobs = []
        for job in jobs:
            if job.get("repository_id") == repo_id:
                job_name = job.get("name")
                if job_name:
                    storage.delete_job(job_name)
                    deleted_jobs.append(job_name)
                    logger.info(f"[DELETE REPO] Deleted associated job: {job_name}")

        # Also delete any hypervisor jobs that use this repository
        hv_jobs = storage.list_hypervisor_jobs()
        deleted_hv_jobs = []
        for hv_job in hv_jobs:
            if hv_job.get("repository_id") == repo_id:
                hv_job_id = hv_job.get("id")
                if hv_job_id:
                    storage.delete_hypervisor_job(hv_job_id)
                    deleted_hv_jobs.append(hv_job.get("name", hv_job_id))
                    logger.info(f"[DELETE REPO] Deleted associated hypervisor job: {hv_job.get('name', hv_job_id)}")

        # Now delete the repository itself
        if not storage.delete_repository(repo_id):
            raise HTTPException(status_code=404, detail="Repository not found")

        return {
            "status": "deleted",
            "deleted_jobs": deleted_jobs,
            "deleted_hypervisor_jobs": deleted_hv_jobs,
        }

    @app.post("/api/v1/repositories/{repo_id}/test")
    def test_repository(repo_id: str, storage: Storage = Depends(get_storage)) -> dict[str, Any]:
        """Test connection to a repository.

        This runs as a background task to avoid blocking the UI.
        Returns a task_id that can be polled for results.
        """
        repo = storage.get_repository(repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found")

        repo_name = repo.get("name", repo_id)
        repo_type = repo.get("repo_type", "smb")
        server = repo.get("server", "")
        share = repo.get("share", "")
        username = repo.get("username")
        password = (
            storage.get_storage_password(repo_id) if repo_type == "smb" else storage.get_repository_password(repo_id)
        )
        domain = repo.get("domain")

        def test_connection(task: Task) -> dict[str, Any]:
            """Background task to test repository connection."""
            from backer.server.repositories import LocalBrowser, NFSBrowser, SMBBrowser

            task.message = f"Testing connection to {server}..."
            task.progress = 20

            success = False
            message = ""

            try:
                if repo_type == "smb":
                    task.message = f"Connecting to SMB share //{server}/{share}..."
                    task.progress = 40
                    success, message = SMBBrowser.test_connection(
                        server=server,
                        share=share,
                        username=username,
                        password=password,
                        domain=domain,
                    )
                elif repo_type == "nfs":
                    task.message = f"Connecting to NFS server {server}..."
                    task.progress = 40
                    success, result = NFSBrowser.list_exports(server)
                    if success:
                        message = f"NFS server responding, {len(result)} exports available"
                    else:
                        message = result
                elif repo_type == "local":
                    # For local repos, the path is stored in 'share' field
                    local_path = share or repo.get("path", "/")
                    task.message = f"Testing local path {local_path}..."
                    task.progress = 40
                    success, message = LocalBrowser.test_connection(local_path)
                elif repo_type == "s3":
                    from backer.backends.base import BackupDestination
                    from backer.backends.kopia import KopiaBackend
                    from backer.backends.s3 import kopia_s3_config

                    s3 = {
                        **repo.get("config", {}).get("s3", {}),
                        **(storage.get_repository_provider_credentials(repo_id) or {}),
                    }
                    success, message = KopiaBackend({"repository_password": password, "s3": s3}).test_connection(
                        BackupDestination(path=kopia_s3_config(s3)["repository"])
                    )
                else:
                    success, message = False, "Test not supported for this repository type"

                task.progress = 80

                # Update status in database
                storage.update_repository_status(repo_id, "connected" if success else "error")

            except Exception as e:
                success = False
                message = str(e)
                logger.exception(f"Repository test failed: {e}")

            return {"success": success, "message": message, "repo_id": repo_id}

        task_manager = get_task_manager()
        task = task_manager.submit(
            task_type="test_repository",
            description=f"Testing connection to '{repo_name}'",
            func=test_connection,
        )

        return {"task_id": task.id, "status": "testing", "message": "Connection test started"}

    @app.post("/api/v1/repositories/{repo_id}/password")
    async def set_repository_password(
        repo_id: str, request: Request, storage: Storage = Depends(get_storage)
    ) -> dict[str, Any]:
        """Set the encryption password on a repository.

        Lets an existing repository that predates mandatory encryption
        passwords (or otherwise lost its password) be given one so it can be
        opened again. If a password is already set, the caller must pass
        `force: true` - changing it does not re-encrypt anything, so a wrong
        value simply makes a working repository unopenable.
        """
        repo = storage.get_repository(repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found")

        data = await request.json()
        password = _repository_password_or_error(data.get("repository_password"))

        if storage.get_repository_password(repo_id) and not data.get("force"):
            raise HTTPException(
                status_code=409,
                detail="Repository already has a password; pass force=true to replace it",
            )

        storage.set_repository_password(repo_id, password)
        logger.info(f"[SET REPO PASSWORD] Password set for repository {repo_id}")

        return {"status": "updated", "repo_id": repo_id}

    @app.get("/api/v1/repositories/{repo_id}/stats")
    def get_repository_stats(repo_id: str, storage: Storage = Depends(get_storage)) -> dict[str, Any]:
        """Get storage statistics for a repository.

        Returns disk space usage information (used, available, total).
        """
        import subprocess
        import tempfile

        repo = storage.get_repository(repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found")

        repo_type = repo.get("repo_type", "smb")
        server = repo.get("server", "")
        share = repo.get("share", "")
        username = repo.get("username")
        password = storage.get_storage_password(repo_id)
        domain = repo.get("domain")

        try:
            if repo_type == "smb":
                # Use smbclient to get disk info
                if username:
                    if domain:
                        auth_str = f"{domain}\\{username}%{password or ''}"
                    else:
                        auth_str = f"{username}%{password or ''}"
                    auth_parts = ["-U", auth_str]
                else:
                    auth_parts = ["-N"]

                cmd = ["smbclient", f"//{server}/{share}", *auth_parts, "-c", "du"]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

                if result.returncode == 0:
                    # Parse smbclient du output
                    # Format: "        xxxxx blocks of size yyyy. zzzzz blocks available"
                    output = result.stdout + result.stderr
                    import re

                    match = re.search(r"(\d+)\s+blocks\s+of\s+size\s+(\d+)\.\s+(\d+)\s+blocks\s+available", output)
                    if match:
                        total_blocks = int(match.group(1))
                        block_size = int(match.group(2))
                        avail_blocks = int(match.group(3))

                        total_bytes = total_blocks * block_size
                        avail_bytes = avail_blocks * block_size
                        used_bytes = total_bytes - avail_bytes

                        return {
                            "repo_id": repo_id,
                            "used_bytes": used_bytes,
                            "available_bytes": avail_bytes,
                            "total_bytes": total_bytes,
                            "used_percent": round((used_bytes / total_bytes * 100), 1) if total_bytes > 0 else 0,
                        }

                return {"repo_id": repo_id, "error": "Could not retrieve stats"}

            elif repo_type == "nfs":
                # Mount temporarily and use df
                mount_point = tempfile.mkdtemp(prefix="backer_stats_")
                mounted = False

                try:
                    mount_cmd = [
                        "sudo",
                        "-n",
                        "mount",
                        "-t",
                        "nfs",
                        "-o",
                        "soft,timeo=30,retrans=2,ro",
                        f"{server}:{share}",
                        mount_point,
                    ]
                    result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=30)
                    if result.returncode != 0:
                        return {"repo_id": repo_id, "error": "Could not mount NFS share"}

                    mounted = True

                    # Use df to get stats
                    df_result = subprocess.run(["df", "-B1", mount_point], capture_output=True, text=True, timeout=10)

                    if df_result.returncode == 0:
                        lines = df_result.stdout.strip().split("\n")
                        if len(lines) >= 2:
                            # Parse df output: Filesystem 1B-blocks Used Available Use% Mounted
                            parts = lines[1].split()
                            if len(parts) >= 4:
                                total_bytes = int(parts[1])
                                used_bytes = int(parts[2])
                                avail_bytes = int(parts[3])

                                pct = round((used_bytes / total_bytes * 100), 1) if total_bytes > 0 else 0
                                return {
                                    "repo_id": repo_id,
                                    "used_bytes": used_bytes,
                                    "available_bytes": avail_bytes,
                                    "total_bytes": total_bytes,
                                    "used_percent": pct,
                                }

                    return {"repo_id": repo_id, "error": "Could not parse df output"}

                finally:
                    if mounted:
                        try:
                            subprocess.run(["sudo", "-n", "umount", "-l", mount_point], capture_output=True, timeout=10)
                        except Exception:
                            pass
                    try:
                        Path(mount_point).rmdir()
                    except Exception:
                        pass

            elif repo_type == "local":
                # Use df for local path
                local_path = repo.get("share", "") or repo.get("path", "")
                if not local_path or not Path(local_path).exists():
                    return {"repo_id": repo_id, "error": "Local path not found"}

                df_result = subprocess.run(["df", "-B1", local_path], capture_output=True, text=True, timeout=10)

                if df_result.returncode == 0:
                    lines = df_result.stdout.strip().split("\n")
                    if len(lines) >= 2:
                        parts = lines[1].split()
                        if len(parts) >= 4:
                            total_bytes = int(parts[1])
                            used_bytes = int(parts[2])
                            avail_bytes = int(parts[3])

                            return {
                                "repo_id": repo_id,
                                "used_bytes": used_bytes,
                                "available_bytes": avail_bytes,
                                "total_bytes": total_bytes,
                                "used_percent": round((used_bytes / total_bytes * 100), 1) if total_bytes > 0 else 0,
                            }

                return {"repo_id": repo_id, "error": "Could not get local stats"}

            else:
                return {"repo_id": repo_id, "error": f"Stats not supported for {repo_type}"}

        except subprocess.TimeoutExpired:
            return {"repo_id": repo_id, "error": "Timeout getting stats"}
        except Exception as e:
            logger.warning(f"Failed to get repository stats: {e}")
            return {"repo_id": repo_id, "error": str(e)}

    @app.post("/api/v1/repositories/{repo_id}/scan")
    def scan_repository(repo_id: str, storage: Storage = Depends(get_storage)) -> dict[str, Any]:
        """Scan a repository for existing Backer metadata and backups.

        This runs as a background task to avoid blocking the UI.
        Returns a task_id that can be polled for results.
        """
        repo = storage.get_repository(repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found")

        repo_name = repo.get("name", repo_id)
        repo_type = repo.get("repo_type", "smb")
        subpath = repo.get("path", "")
        server = repo.get("server", "")
        share = repo.get("share", "")
        username = repo.get("username")
        password = (
            storage.get_repository_password(repo_id) if repo_type == "local" else storage.get_storage_password(repo_id)
        )
        domain = repo.get("domain")
        mount_point = repo.get("mount_point")

        # Build display path
        if repo_type == "smb":
            display_path = f"//{server}/{share}"
        elif repo_type == "nfs":
            display_path = f"{server}:{share}"
        else:
            display_path = share
        if subpath:
            display_path = f"{display_path}/{subpath}"

        def scan_repo_task(task: Task) -> dict[str, Any]:
            """Background task to scan repository."""
            import json as json_module

            from backer.server.repositories import smb_list_files, smb_read_file

            def format_result(discovery: dict) -> dict[str, Any]:
                """Format discovery result for API response."""
                result = {
                    "success": True,
                    "repository_id": repo_id,
                    "repository_name": repo_name,
                    "path": display_path,
                    "initialized": discovery.get("initialized", False),
                    "summary": discovery.get("summary", {}),
                    "agents": discovery.get("agents", []),
                    "hypervisors": discovery.get("hypervisors", []),
                    "guests": discovery.get("guests", []),
                    "hypervisor_jobs": discovery.get("hypervisor_jobs", []),
                    "jobs": [
                        {
                            "job_name": j.get("job_name"),
                            "run_count": j.get("run_count", 0),
                            "created_at": j.get("created_at"),
                            "updated_at": j.get("updated_at"),
                        }
                        for j in discovery.get("jobs", [])
                    ],
                    "snapshots": [
                        {
                            "snapshot_id": s.get("snapshot_id"),
                            "short_id": s.get("short_id"),
                            "hostname": s.get("hostname"),
                            "time": s.get("time"),
                            "paths": s.get("paths", []),
                        }
                        for s in discovery.get("snapshots", [])[:50]
                    ],
                }
                # Include scan diagnostic info if present
                if discovery.get("scan_path"):
                    result["scan_path"] = discovery["scan_path"]
                if discovery.get("scan_note"):
                    result["scan_note"] = discovery["scan_note"]
                return result

            try:
                task.message = "Connecting to repository..."
                task.progress = 10

                # Use direct filesystem access for local or mounted paths
                if mount_point or repo_type == "local":
                    from backer.core.repo_metadata import RepositoryMetadata

                    if mount_point:
                        repo_path = mount_point
                        if subpath:
                            repo_path = repo_path.rstrip("/") + "/" + subpath
                    else:
                        repo_path = share or repo.get("path", "")

                    task.message = "Reading local metadata..."
                    task.progress = 30
                    repo_meta = RepositoryMetadata(repo_path, repo_type)
                    discovery = repo_meta.discover_all()

                    # For LOCAL repos, also scan Agents/ folder structure and kopia snapshots
                    if repo_type == "local":
                        task.message = "Scanning Agents folder..."
                        task.progress = 40

                        # Scan filesystem for Agents/{job_name}/contents/ structure
                        agents_dir = Path(repo_path) / "Agents"
                        if agents_dir.exists() and agents_dir.is_dir():
                            jobs_from_fs = {}
                            try:
                                for job_dir in agents_dir.iterdir():
                                    if job_dir.is_dir() and not job_dir.name.startswith("."):
                                        job_name = job_dir.name
                                        contents_dir = job_dir / "contents"

                                        # Count files if contents folder exists
                                        file_count = 0
                                        if contents_dir.exists():
                                            file_count = sum(1 for _ in contents_dir.rglob("*") if _.is_file())

                                        if file_count > 0 or contents_dir.exists():
                                            jobs_from_fs[job_name] = {
                                                "job_name": job_name,
                                                "source": "filesystem",
                                                "file_count": file_count,
                                                "path": str(contents_dir),
                                            }
                                            logger.info(f"[SCAN] Found job '{job_name}' ({file_count} files)")

                                # Merge filesystem jobs with existing jobs
                                existing_jobs = {j.get("job_name"): j for j in discovery.get("jobs", [])}
                                for job_name, job_info in jobs_from_fs.items():
                                    if job_name not in existing_jobs:
                                        existing_jobs[job_name] = job_info
                                    else:
                                        # Add filesystem info to existing job
                                        existing_jobs[job_name]["file_count"] = job_info.get("file_count", 0)
                                        existing_jobs[job_name]["path"] = job_info.get("path")
                                discovery["jobs"] = list(existing_jobs.values())
                                discovery["initialized"] = True

                                logger.info(f"[SCAN] Found {len(jobs_from_fs)} jobs in Agents folder")
                            except Exception as fs_err:
                                logger.warning(f"[SCAN] Error scanning Agents folder: {fs_err}")

                        task.message = "Scanning kopia repository..."
                        task.progress = 60
                        try:
                            kopia_password = _repository_password_or_error(password)
                            kopia = ServerKopia(repo_path, kopia_password)

                            # Check if kopia repo exists
                            kopia_repo_path = Path(repo_path) / ".kopia-repo"
                            if kopia_repo_path.exists():
                                logger.info(f"[SCAN] Found kopia repository at {kopia_repo_path}")

                                # List all snapshots (no job filter)
                                snapshots = kopia.snapshot_list(job_name=None)
                                if snapshots:
                                    logger.info(f"[SCAN] Found {len(snapshots)} kopia snapshots")
                                    discovery["initialized"] = True

                                    # Convert to expected format and add to discovery
                                    kopia_snapshots = []
                                    jobs_from_snapshots = {}

                                    for snap in snapshots:
                                        kopia_snapshots.append(
                                            {
                                                "snapshot_id": snap.get("full_id"),
                                                "short_id": snap.get("id"),
                                                "hostname": snap.get("hostname"),
                                                "time": snap.get("timestamp"),
                                                "paths": [snap.get("source", "")],
                                                "job": snap.get("job"),
                                            }
                                        )

                                        # Track jobs found in snapshots
                                        job_name = snap.get("job")
                                        if job_name:
                                            if job_name not in jobs_from_snapshots:
                                                jobs_from_snapshots[job_name] = {
                                                    "job_name": job_name,
                                                    "run_count": 0,
                                                    "source": "kopia",
                                                }
                                            jobs_from_snapshots[job_name]["run_count"] += 1

                                    # Merge kopia snapshots with any existing
                                    existing_snapshots = discovery.get("snapshots", [])
                                    discovery["snapshots"] = existing_snapshots + kopia_snapshots

                                    # Merge jobs from snapshots with existing jobs
                                    existing_jobs = {j.get("job_name"): j for j in discovery.get("jobs", [])}
                                    for job_name, job_info in jobs_from_snapshots.items():
                                        if job_name not in existing_jobs:
                                            existing_jobs[job_name] = job_info
                                        else:
                                            # Update run count
                                            existing_jobs[job_name]["run_count"] = max(
                                                existing_jobs[job_name].get("run_count", 0), job_info["run_count"]
                                            )
                                    discovery["jobs"] = list(existing_jobs.values())

                                    # Update summary
                                    summary = discovery.get("summary", {})
                                    summary["snapshot_count"] = len(discovery["snapshots"])
                                    summary["job_count"] = len(discovery["jobs"])
                                    discovery["summary"] = summary
                            else:
                                logger.info(f"[SCAN] No kopia repository found at {repo_path}")
                        except Exception as kopia_err:
                            logger.warning(f"[SCAN] Error scanning kopia repository: {kopia_err}")

                    return format_result(discovery)

                # For SMB shares, use smbclient to read metadata directly
                elif repo_type == "smb":
                    # Scan job subfolders for metadata
                    # Each job has its own subfolder: repo_root/job_name/.backer/
                    base_path = subpath if subpath else ""

                    task.message = "Listing job subfolders..."
                    task.progress = 15
                    logger.info(f"[SCAN] Listing job subfolders at //{server}/{share}/{base_path}")

                    all_agents = []
                    all_jobs = []
                    all_snapshots = []
                    found_any_metadata = False
                    agent_ids_seen = set()

                    # List directories at the repo root (legacy: job subfolders at root)
                    ok, entries = smb_list_files(server, share, base_path, username, password, domain)

                    # Also scan inside Agents/ folder (new structure)
                    agents_path = f"{base_path}/Agents" if base_path else "Agents"
                    ok_agents_folder, agent_entries = smb_list_files(
                        server, share, agents_path, username, password, domain
                    )

                    # Build combined list of folders to scan with their base paths
                    folders_to_scan = []
                    if ok:
                        # Filter to get just directory names (legacy job subfolders at root)
                        legacy_folders = [
                            e
                            for e in entries
                            if not e.startswith(".")
                            and not e.endswith(".json")
                            and e != "Agents"
                            and e != "Hypervisors"
                        ]
                        for folder in legacy_folders:
                            folder_path = f"{base_path}/{folder}" if base_path else folder
                            folders_to_scan.append((folder, folder_path))

                    if ok_agents_folder:
                        # Job subfolders under Agents/
                        agent_job_folders = [
                            e for e in agent_entries if not e.startswith(".") and not e.endswith(".json")
                        ]
                        for folder in agent_job_folders:
                            folder_path = f"{agents_path}/{folder}"
                            folders_to_scan.append((folder, folder_path))
                        logger.info(f"[SCAN] Found {len(agent_job_folders)} job folders under Agents/")

                    if folders_to_scan:
                        total_folders = len(folders_to_scan)
                        for idx, (job_folder, folder_path) in enumerate(folders_to_scan):
                            progress_pct = 20 + int((idx / max(total_folders, 1)) * 60)
                            task.progress = progress_pct
                            task.message = f"Scanning job folder: {job_folder}..."

                            metadata_base = f"{folder_path}/.backer"

                            # Check if this folder has .backer metadata
                            ok2, content = smb_read_file(
                                server, share, f"{metadata_base}/metadata.json", username, password, domain
                            )

                            if not ok2:
                                # No metadata in this folder, skip
                                continue

                            found_any_metadata = True
                            logger.info(f"[SCAN] Found metadata in job folder: {job_folder}")

                            # Read agents (dedup by agent_id)
                            ok3, agent_files = smb_list_files(
                                server, share, f"{metadata_base}/agents", username, password, domain
                            )
                            if ok3:
                                for f in agent_files:
                                    if f.endswith(".json"):
                                        ok4, c = smb_read_file(
                                            server, share, f"{metadata_base}/agents/{f}", username, password, domain
                                        )
                                        if ok4:
                                            try:
                                                agent = json_module.loads(c)
                                                agent_id = agent.get("agent_id")
                                                if agent_id and agent_id not in agent_ids_seen:
                                                    agent_ids_seen.add(agent_id)
                                                    all_agents.append(agent)
                                            except json_module.JSONDecodeError:
                                                pass

                            # Read jobs
                            ok3, job_dirs = smb_list_files(
                                server, share, f"{metadata_base}/jobs", username, password, domain
                            )
                            if ok3:
                                for d in job_dirs:
                                    ok4, c = smb_read_file(
                                        server,
                                        share,
                                        f"{metadata_base}/jobs/{d}/config.json",
                                        username,
                                        password,
                                        domain,
                                    )
                                    if ok4:
                                        try:
                                            job = json_module.loads(c)
                                            # Add job_folder context
                                            job["job_folder"] = job_folder
                                            ok5, runs = smb_list_files(
                                                server,
                                                share,
                                                f"{metadata_base}/jobs/{d}/runs",
                                                username,
                                                password,
                                                domain,
                                            )
                                            run_count = len([r for r in runs if r.endswith(".json")]) if ok5 else 0
                                            job["run_count"] = run_count
                                            all_jobs.append(job)
                                        except json_module.JSONDecodeError:
                                            pass

                            # Read snapshots
                            ok3, snap_files = smb_list_files(
                                server, share, f"{metadata_base}/snapshots", username, password, domain
                            )
                            if ok3:
                                for f in snap_files:
                                    if f.endswith(".json"):
                                        ok4, c = smb_read_file(
                                            server, share, f"{metadata_base}/snapshots/{f}", username, password, domain
                                        )
                                        if ok4:
                                            try:
                                                snap = json_module.loads(c)
                                                snap["job_folder"] = job_folder
                                                all_snapshots.append(snap)
                                            except json_module.JSONDecodeError:
                                                pass

                    # Also scan Hypervisors folder specifically for VM backups
                    all_hypervisors = []
                    all_guests = []
                    all_hypervisor_jobs = []
                    hypervisors_path = f"{base_path}/Hypervisors" if base_path else "Hypervisors"
                    ok_hv, hv_folders = smb_list_files(server, share, hypervisors_path, username, password, domain)
                    if ok_hv:
                        for hv_folder in hv_folders:
                            if hv_folder.startswith("."):
                                continue
                            task.message = f"Scanning hypervisor: {hv_folder}..."
                            hv_metadata_base = f"{hypervisors_path}/{hv_folder}/.backer"

                            # Read hypervisor metadata
                            ok_meta, meta_content = smb_read_file(
                                server, share, f"{hv_metadata_base}/metadata.json", username, password, domain
                            )
                            if ok_meta:
                                found_any_metadata = True
                                logger.info(f"[SCAN] Found hypervisor metadata: {hv_folder}")

                                # Read hypervisors
                                ok_hvs, hv_files = smb_list_files(
                                    server, share, f"{hv_metadata_base}/hypervisors", username, password, domain
                                )
                                if ok_hvs:
                                    for f in hv_files:
                                        if f.endswith(".json"):
                                            ok_h, c = smb_read_file(
                                                server,
                                                share,
                                                f"{hv_metadata_base}/hypervisors/{f}",
                                                username,
                                                password,
                                                domain,
                                            )
                                            if ok_h:
                                                try:
                                                    hv_data = json_module.loads(c)
                                                    hv_data["folder"] = hv_folder
                                                    all_hypervisors.append(hv_data)
                                                except json_module.JSONDecodeError:
                                                    pass

                                # Read hypervisor jobs
                                ok_jobs, job_files = smb_list_files(
                                    server, share, f"{hv_metadata_base}/hypervisor_jobs", username, password, domain
                                )
                                if ok_jobs:
                                    for f in job_files:
                                        if f.endswith(".json"):
                                            ok_j, c = smb_read_file(
                                                server,
                                                share,
                                                f"{hv_metadata_base}/hypervisor_jobs/{f}",
                                                username,
                                                password,
                                                domain,
                                            )
                                            if ok_j:
                                                try:
                                                    job_data = json_module.loads(c)
                                                    job_data["hypervisor_folder"] = hv_folder
                                                    all_hypervisor_jobs.append(job_data)
                                                except json_module.JSONDecodeError:
                                                    pass

                                # Read guests from hypervisor_backups
                                ok_guests, guest_dirs = smb_list_files(
                                    server, share, f"{hv_metadata_base}/hypervisor_backups", username, password, domain
                                )
                                guest_count = len(guest_dirs) if ok_guests else 0
                                logger.debug(f"[SCAN] Found {guest_count} guest directories in {hv_folder}")
                                if ok_guests:
                                    for vmid_dir in guest_dirs:
                                        logger.debug(f"[SCAN] Reading guest from folder: {vmid_dir}")
                                        guest_path = f"{hv_metadata_base}/hypervisor_backups/{vmid_dir}/guest.json"
                                        ok_g, g_content = smb_read_file(
                                            server, share, guest_path, username, password, domain
                                        )
                                        if ok_g:
                                            try:
                                                guest_data = json_module.loads(g_content)
                                                vmid = guest_data.get("vmid")
                                                name = guest_data.get("name")
                                                logger.debug(f"[SCAN] Guest data: vmid={vmid}, name={name}")
                                                guest_data["hypervisor_folder"] = hv_folder
                                                # Count backup runs
                                                runs_path = f"{hv_metadata_base}/hypervisor_backups/{vmid_dir}/runs"
                                                ok_runs, run_files = smb_list_files(
                                                    server, share, runs_path, username, password, domain
                                                )
                                                run_count = len([r for r in run_files if r.endswith(".json")])
                                                guest_data["run_count"] = run_count if ok_runs else 0
                                                all_guests.append(guest_data)
                                                vmid = guest_data.get("vmid")
                                                name = guest_data.get("name")
                                                logger.info(
                                                    f"[SCAN] Added guest {vmid} ({name}) with {run_count} backups"
                                                )
                                            except json_module.JSONDecodeError as e:
                                                logger.error(f"[SCAN] Failed to parse guest.json for {vmid_dir}: {e}")
                                        else:
                                            logger.warning(f"[SCAN] Failed to read guest.json for {vmid_dir}")

                    if not found_any_metadata:
                        # Also check for legacy metadata at root level (backwards compatibility)
                        metadata_base = f"{base_path}/.backer" if base_path else ".backer"
                        ok, content = smb_read_file(
                            server, share, f"{metadata_base}/metadata.json", username, password, domain
                        )
                        if not ok:
                            logger.info(f"[SCAN] No metadata found in //{server}/{share}/{base_path}")
                            return format_result(
                                {
                                    "initialized": False,
                                    "agents": [],
                                    "jobs": [],
                                    "snapshots": [],
                                    "summary": {"agent_count": 0, "job_count": 0, "snapshot_count": 0, "total_runs": 0},
                                    "scan_path": f"//{server}/{share}/{base_path}",
                                    "scan_note": "Scanned job subfolders for .backer/metadata.json - none found",
                                }
                            )

                    task.progress = 90
                    return format_result(
                        {
                            "initialized": found_any_metadata,
                            "agents": all_agents,
                            "jobs": all_jobs,
                            "snapshots": all_snapshots,
                            "hypervisors": all_hypervisors,
                            "guests": all_guests,
                            "hypervisor_jobs": all_hypervisor_jobs,
                            "summary": {
                                "agent_count": len(all_agents),
                                "job_count": len(all_jobs),
                                "snapshot_count": len(all_snapshots),
                                "hypervisor_count": len(all_hypervisors),
                                "guest_count": len(all_guests),
                                "hypervisor_job_count": len(all_hypervisor_jobs),
                                "total_runs": sum(j.get("run_count", 0) for j in all_jobs),
                                "total_vm_runs": sum(g.get("run_count", 0) for g in all_guests),
                            },
                        }
                    )

                elif repo_type == "nfs":
                    # NFS requires temporary mount to scan
                    import subprocess
                    import tempfile
                    from pathlib import Path as ScanPath

                    from backer.core.repo_metadata import RepositoryMetadata

                    task.message = "Mounting NFS share..."
                    task.progress = 20

                    temp_mount = ScanPath(tempfile.mkdtemp(prefix="backer_nfs_scan_"))
                    mounted = False

                    try:
                        # Mount NFS share with soft options to prevent hangs
                        nfs_opts = "soft,timeo=50,retrans=2"
                        mount_cmd = ["mount", "-t", "nfs", "-o", nfs_opts, f"{server}:{share}", str(temp_mount)]
                        result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=30)

                        # If permission error, try with sudo
                        if result.returncode != 0:
                            error_msg = result.stderr.strip().lower()
                            if any(err in error_msg for err in ("permission", "setuid", "user", "fstab")):
                                nfs_target = f"{server}:{share}"
                                mount_cmd = [
                                    "sudo",
                                    "-n",
                                    "mount",
                                    "-t",
                                    "nfs",
                                    "-o",
                                    nfs_opts,
                                    nfs_target,
                                    str(temp_mount),
                                ]
                                result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=30)

                        if result.returncode != 0:
                            return {
                                "success": False,
                                "error": f"Failed to mount NFS share: {result.stderr.strip()}",
                                "repository_id": repo_id,
                                "repository_name": repo_name,
                                "path": display_path,
                                "hint": "Ensure NFS is accessible and backer has mount permissions",
                            }

                        mounted = True
                        task.message = "Scanning NFS repository..."
                        task.progress = 40

                        # Build full path with subpath if specified
                        scan_path = str(temp_mount)
                        if subpath:
                            scan_path = scan_path.rstrip("/") + "/" + subpath

                        try:
                            repo_meta = RepositoryMetadata(scan_path, repo_type)
                            return format_result(repo_meta.discover_all())
                        except PermissionError as perm_err:
                            return {
                                "success": False,
                                "error": f"Permission denied reading NFS files: {perm_err}",
                                "repository_id": repo_id,
                                "repository_name": repo_name,
                                "path": display_path,
                                "hint": (
                                    "NFS permission issue. The container user (UID 1000) "
                                    "cannot read files on the share. Solutions:\n"
                                    "1. On NFS server: chmod -R o+r on backup files\n"
                                    "2. Use 'all_squash,anonuid=1000' in NFS exports\n"
                                    "3. Run container with matching UID via --user flag"
                                ),
                            }

                    finally:
                        # Always cleanup: unmount and remove temp dir
                        if mounted:
                            try:
                                subprocess.run(["umount", str(temp_mount)], capture_output=True, timeout=10)
                            except Exception:
                                try:
                                    subprocess.run(
                                        ["sudo", "-n", "umount", str(temp_mount)], capture_output=True, timeout=10
                                    )
                                except Exception:
                                    pass
                        try:
                            temp_mount.rmdir()
                        except Exception:
                            pass

                else:
                    return {
                        "success": False,
                        "error": f"Unsupported repository type: {repo_type}",
                        "repository_id": repo_id,
                        "repository_name": repo_name,
                        "path": display_path,
                    }

            except Exception as e:
                logger.error(f"Failed to scan repository {repo_id}: {e}")
                return {
                    "success": False,
                    "error": str(e),
                    "repository_id": repo_id,
                }

        task_manager = get_task_manager()
        task = task_manager.submit(
            task_type="scan_repository",
            description=f"Scanning repository '{repo_name}'",
            func=scan_repo_task,
        )

        return {"task_id": task.id, "status": "scanning", "message": "Repository scan started"}

    @app.post("/api/v1/repositories/{repo_id}/import")
    def import_repository_metadata_endpoint(
        repo_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Import discovered metadata from repository into server database.

        This imports jobs, runs, and agent references from the repository
        metadata into the server's database, enabling management of
        previously-created backups.

        For SMB shares, uses smbclient to read files directly (no root needed).
        For local/mounted paths, accesses the filesystem directly.
        """
        import json as json_module
        from datetime import datetime as dt

        from backer.server.repositories import smb_list_files, smb_read_file

        repo = storage.get_repository(repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found")

        repo_type = repo.get("repo_type", "smb")
        subpath = repo.get("path", "")
        server = repo.get("server", "")
        share = repo.get("share", "")
        username = repo.get("username")
        password = storage.get_storage_password(repo_id)
        domain = repo.get("domain")

        try:
            # Use direct filesystem access for local or mounted paths
            if repo.get("mount_point") or repo_type == "local":
                from backer.server.repo_metadata import import_repository_metadata

                if repo.get("mount_point"):
                    repo_path = repo["mount_point"]
                    if subpath:
                        repo_path = repo_path.rstrip("/") + "/" + subpath
                else:
                    repo_path = share or repo.get("path", "")

                result = import_repository_metadata(repo_path, storage, repo_id)

                # For LOCAL repos, also import jobs from kopia snapshots
                if repo_type == "local":
                    try:
                        kopia_password = _repository_password_or_error(storage.get_repository_password(repo_id))
                        kopia = ServerKopia(repo_path, kopia_password)

                        kopia_repo_path = Path(repo_path) / ".kopia-repo"
                        if kopia_repo_path.exists():
                            # List all snapshots to find jobs
                            snapshots = kopia.snapshot_list(job_name=None)
                            jobs_imported = 0

                            # Group snapshots by job name
                            jobs_from_snapshots = {}
                            for snap in snapshots:
                                job_name = snap.get("job")
                                if job_name and job_name not in jobs_from_snapshots:
                                    jobs_from_snapshots[job_name] = {
                                        "source": snap.get("source", ""),
                                        "snapshot_count": 0,
                                        "latest_snapshot": snap.get("full_id"),
                                    }
                                if job_name:
                                    jobs_from_snapshots[job_name]["snapshot_count"] += 1

                            # Create jobs that don't already exist
                            for job_name, job_info in jobs_from_snapshots.items():
                                existing = storage.get_job(job_name)
                                if not existing:
                                    job_config = {
                                        "repository_id": repo_id,
                                        "source_path": job_info["source"],
                                        "destination_path": repo_path,
                                        "imported_at": tz.get_now().isoformat(),
                                        "imported_from_kopia": True,
                                        "snapshot_count": job_info["snapshot_count"],
                                    }
                                    storage.save_job(job_name, job_config)
                                    jobs_imported += 1
                                    logger.info(f"[IMPORT] Created job '{job_name}' from kopia snapshots")

                            if jobs_imported > 0:
                                result["imported"] = result.get("imported", {})
                                result["imported"]["kopia_jobs"] = jobs_imported
                                # Update total jobs count
                                result["imported"]["jobs"] = result["imported"].get("jobs", 0) + jobs_imported
                                logger.info(f"[IMPORT] Imported {jobs_imported} jobs from kopia snapshots")

                    except Exception as kopia_err:
                        logger.warning(f"[IMPORT] Error importing from kopia: {kopia_err}")

                return {
                    "repository_id": repo_id,
                    "repository_name": repo.get("name"),
                    **result,
                }

            # For SMB shares, use smbclient to read metadata directly
            elif repo_type == "smb":
                base_path = subpath if subpath else ""
                imported = {"agents": 0, "jobs": 0, "runs": 0}
                agent_ids_seen = set()

                # Build the repository path for imported jobs
                repo_path = f"//{server}/{share}"
                if subpath:
                    repo_path = f"{repo_path}/{subpath}"

                # Helper function to import from a job folder's .backer directory
                def import_from_metadata_base(metadata_base: str, job_folder: str) -> None:
                    # Read and import jobs from this metadata location
                    ok, job_dirs = smb_list_files(server, share, f"{metadata_base}/jobs", username, password, domain)
                    if ok:
                        for job_dir in job_dirs:
                            ok2, job_content = smb_read_file(
                                server, share, f"{metadata_base}/jobs/{job_dir}/config.json", username, password, domain
                            )
                            if ok2:
                                try:
                                    job_data = json_module.loads(job_content)
                                    job_name = job_data.get("job_name")
                                    config = job_data.get("config", {})

                                    if job_name:
                                        existing = storage.get_job(job_name)
                                        if not existing:
                                            config.pop("backend", None)
                                            config.pop("backend_type", None)
                                            config.pop("backend_options", None)
                                            config["repository_id"] = repo_id
                                            config["imported_at"] = tz.get_now().isoformat()
                                            config["imported_from_repo"] = True
                                            config["job_folder"] = job_folder
                                            # Set destination_path to repo path for restores
                                            if not config.get("destination_path"):
                                                config["destination_path"] = repo_path
                                            storage.save_job(job_name, config)
                                            imported["jobs"] += 1

                                            # Import job runs
                                            ok3, run_files = smb_list_files(
                                                server,
                                                share,
                                                f"{metadata_base}/jobs/{job_dir}/runs",
                                                username,
                                                password,
                                                domain,
                                            )
                                            if ok3:
                                                for run_file in run_files:
                                                    if run_file.endswith(".json"):
                                                        ok4, run_content = smb_read_file(
                                                            server,
                                                            share,
                                                            f"{metadata_base}/jobs/{job_dir}/runs/{run_file}",
                                                            username,
                                                            password,
                                                            domain,
                                                        )
                                                        if ok4:
                                                            try:
                                                                run = json_module.loads(run_content)
                                                                run_id = run.get("run_id")
                                                                if run_id:
                                                                    started_at = tz.get_now()
                                                                    finished_at = None
                                                                    if run.get("started_at"):
                                                                        try:
                                                                            started_at = dt.fromisoformat(
                                                                                run["started_at"]
                                                                            )
                                                                        except (ValueError, TypeError):
                                                                            pass
                                                                    if run.get("finished_at"):
                                                                        try:
                                                                            finished_at = dt.fromisoformat(
                                                                                run["finished_at"]
                                                                            )
                                                                        except (ValueError, TypeError):
                                                                            pass
                                                                    storage.save_job_run(
                                                                        run_id=run_id,
                                                                        job_name=job_name,
                                                                        status=run.get("status", "unknown"),
                                                                        started_at=started_at,
                                                                        finished_at=finished_at,
                                                                        bytes_transferred=run.get(
                                                                            "bytes_transferred", 0
                                                                        ),
                                                                        files_transferred=run.get(
                                                                            "files_transferred", 0
                                                                        ),
                                                                        snapshot_id=run.get("snapshot_id"),
                                                                    )
                                                                    imported["runs"] += 1
                                                            except json_module.JSONDecodeError:
                                                                pass
                                except json_module.JSONDecodeError:
                                    pass

                    # Read agents (dedup by agent_id)
                    ok, agent_files = smb_list_files(
                        server, share, f"{metadata_base}/agents", username, password, domain
                    )
                    if ok:
                        for f in agent_files:
                            if f.endswith(".json"):
                                ok2, c = smb_read_file(
                                    server, share, f"{metadata_base}/agents/{f}", username, password, domain
                                )
                                if ok2:
                                    try:
                                        agent = json_module.loads(c)
                                        agent_id = agent.get("agent_id")
                                        if agent_id and agent_id not in agent_ids_seen:
                                            agent_ids_seen.add(agent_id)
                                            imported["agents"] += 1
                                    except json_module.JSONDecodeError:
                                        pass

                found_any_metadata = False

                # First try legacy structure: root/.backer/
                legacy_metadata_base = f"{base_path}/.backer" if base_path else ".backer"
                ok, _ = smb_read_file(
                    server, share, f"{legacy_metadata_base}/metadata.json", username, password, domain
                )
                if ok:
                    found_any_metadata = True
                    logger.info("[IMPORT] Found legacy metadata at root level")
                    import_from_metadata_base(legacy_metadata_base, "")

                # Scan Agents/ folder for job-specific metadata (new structure)
                agents_path = f"{base_path}/Agents" if base_path else "Agents"
                ok, agent_folders = smb_list_files(server, share, agents_path, username, password, domain)
                if ok:
                    for job_folder in agent_folders:
                        if job_folder.startswith("."):
                            continue
                        folder_path = f"{agents_path}/{job_folder}"
                        metadata_base = f"{folder_path}/.backer"

                        # Check if this folder has metadata
                        ok2, _ = smb_read_file(
                            server, share, f"{metadata_base}/metadata.json", username, password, domain
                        )
                        if ok2:
                            found_any_metadata = True
                            logger.info(f"[IMPORT] Found metadata in Agents/{job_folder}")
                            import_from_metadata_base(metadata_base, job_folder)

                if not found_any_metadata:
                    return {"error": "Repository has no Backer metadata"}

                logger.info(
                    f"[IMPORT] Imported {imported['jobs']} jobs, {imported['runs']} runs, "
                    f"discovered {imported['agents']} agents"
                )

                return {
                    "success": True,
                    "repository_id": repo_id,
                    "repository_name": repo.get("name"),
                    "imported": imported,
                }

            else:
                # NFS without mount_point - not supported
                return {
                    "success": False,
                    "error": "NFS import requires mount_point to be set",
                    "repository_id": repo_id,
                    "repository_name": repo.get("name"),
                    "hint": "Mount the NFS share and set the mount_point field",
                }

        except Exception as e:
            logger.error(f"Failed to import metadata from repository {repo_id}: {e}")
            return {
                "success": False,
                "error": str(e),
                "repository_id": repo_id,
            }

    @app.post("/api/v1/repositories/{repo_id}/import-hypervisor-jobs")
    def import_hypervisor_jobs_endpoint(
        repo_id: str,
        hypervisor_id: str | None = None,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Import hypervisor job configurations from repository metadata.

        Scans the repository for hypervisor backup metadata and imports job
        configurations. This enables recovery of job configurations after
        reinstalling the Backer server.

        The metadata is stored in: {repo}/Hypervisors/{hypervisor_name}/.backer/

        Args:
            repo_id: Repository ID to scan
            hypervisor_id: Optional - only import jobs for this hypervisor
        """
        import json as json_module
        import subprocess
        import tempfile

        from backer.server.repositories import smb_list_files, smb_read_file

        repo = storage.get_repository(repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found")

        repo_type = repo.get("repo_type", "smb")
        subpath = repo.get("path", "")
        server = repo.get("server", "")
        share = repo.get("share", "")
        username = repo.get("username")
        password = storage.get_storage_password(repo_id)
        domain = repo.get("domain")

        imported = {"hypervisors": 0, "jobs": 0, "guests": 0, "skipped_jobs": 0}
        errors: list[str] = []

        try:
            if repo_type == "smb":
                # Build auth for smbclient
                auth_parts = []
                if username:
                    pw = password or ""
                    if domain:
                        auth_parts.extend(["-U", f"{domain}\\{username}%{pw}"])
                    else:
                        auth_parts.extend(["-U", f"{username}%{pw}"])
                else:
                    auth_parts.extend(["-N"])

                # List hypervisors in Hypervisors/ directory
                hypervisors_path = f"{subpath}/Hypervisors" if subpath else "Hypervisors"
                ok, hv_dirs = smb_list_files(server, share, hypervisors_path, username, password, domain)

                if not ok:
                    return {
                        "success": False,
                        "error": "No Hypervisors directory found in repository",
                        "repository_id": repo_id,
                    }

                for hv_dir in hv_dirs:
                    if hv_dir.startswith("."):
                        continue

                    # Read hypervisor jobs from .backer/hypervisor_jobs/
                    jobs_path = f"{hypervisors_path}/{hv_dir}/.backer/hypervisor_jobs"
                    ok2, job_files = smb_list_files(server, share, jobs_path, username, password, domain)

                    if not ok2:
                        continue

                    for job_file in job_files:
                        if not job_file.endswith(".json"):
                            continue

                        ok3, job_content = smb_read_file(
                            server, share, f"{jobs_path}/{job_file}", username, password, domain
                        )

                        if not ok3:
                            continue

                        try:
                            job_data = json_module.loads(job_content)
                            job_id = job_data.get("job_id")
                            job_name = job_data.get("name")
                            job_hv_id = job_data.get("hypervisor_id")

                            if not job_id or not job_name:
                                continue

                            # Filter by hypervisor_id if specified
                            if hypervisor_id and job_hv_id != hypervisor_id:
                                continue

                            # Check if job already exists
                            existing = storage.get_hypervisor_job(job_id)
                            if existing:
                                imported["skipped_jobs"] += 1
                                continue

                            # Check if job name already exists
                            existing_by_name = storage.get_hypervisor_job_by_name(job_name)
                            if existing_by_name:
                                imported["skipped_jobs"] += 1
                                continue

                            # Check if the hypervisor exists
                            hv = storage.get_hypervisor(job_hv_id)
                            if not hv:
                                # Try to find hypervisor by name or host
                                hv_name = job_data.get("hypervisor_name")
                                hv_host = job_data.get("hypervisor_host")

                                all_hvs = storage.list_hypervisors()
                                for existing_hv in all_hvs:
                                    if existing_hv["name"] == hv_name or existing_hv["host"] == hv_host:
                                        job_hv_id = existing_hv["id"]
                                        hv = existing_hv
                                        break

                                if not hv:
                                    errors.append(f"Hypervisor not found for job '{job_name}'")
                                    continue

                            # Create the job
                            # guest_ids from metadata is already a list, pass it directly
                            # (add_hypervisor_job handles JSON encoding internally)
                            guest_ids = job_data.get("guest_ids") or []

                            storage.add_hypervisor_job(
                                job_id=job_id,
                                name=job_name,
                                hypervisor_id=job_hv_id,
                                guest_ids=guest_ids,
                                repository_id=repo_id,
                                backup_mode=job_data.get("backup_mode", "snapshot"),
                                compression=job_data.get("compression", "zstd"),
                                schedule_cron=job_data.get("schedule_cron"),
                                enabled=job_data.get("enabled", True),
                                copies_to_keep=job_data.get("copies_to_keep", 0),
                            )
                            imported["jobs"] += 1
                            logger.info(f"Imported hypervisor job '{job_name}' from repository")

                        except json_module.JSONDecodeError as e:
                            errors.append(f"Invalid JSON in {job_file}: {e}")
                        except Exception as e:
                            errors.append(f"Error importing {job_file}: {e}")

                return {
                    "success": True,
                    "repository_id": repo_id,
                    "repository_name": repo.get("name"),
                    "imported": imported,
                    "errors": errors if errors else None,
                }

            elif repo_type == "nfs":
                # NFS: temporarily mount and read
                mount_point = tempfile.mkdtemp(prefix="backer_nfs_import_")
                mounted = False

                try:
                    nfs_export = share or repo.get("path", "")
                    mount_cmd = [
                        "sudo",
                        "-n",
                        "mount",
                        "-t",
                        "nfs",
                        "-o",
                        "soft,timeo=50,retrans=2,ro",
                        f"{server}:{nfs_export}",
                        mount_point,
                    ]
                    result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=60)
                    if result.returncode != 0:
                        return {
                            "success": False,
                            "error": f"Failed to mount NFS: {result.stderr.strip()}",
                            "repository_id": repo_id,
                        }

                    mounted = True

                    # Scan Hypervisors directory
                    hypervisors_dir = Path(mount_point) / "Hypervisors"
                    if not hypervisors_dir.exists():
                        return {
                            "success": False,
                            "error": "No Hypervisors directory found in repository",
                            "repository_id": repo_id,
                        }

                    for hv_dir in hypervisors_dir.iterdir():
                        if not hv_dir.is_dir() or hv_dir.name.startswith("."):
                            continue

                        jobs_dir = hv_dir / ".backer" / "hypervisor_jobs"
                        if not jobs_dir.exists():
                            continue

                        for job_file in jobs_dir.glob("*.json"):
                            try:
                                job_data = json_module.loads(job_file.read_text())
                                job_id = job_data.get("job_id")
                                job_name = job_data.get("name")
                                job_hv_id = job_data.get("hypervisor_id")

                                if not job_id or not job_name:
                                    continue

                                # Filter by hypervisor_id if specified
                                if hypervisor_id and job_hv_id != hypervisor_id:
                                    continue

                                # Check if job already exists
                                existing = storage.get_hypervisor_job(job_id)
                                if existing:
                                    imported["skipped_jobs"] += 1
                                    continue

                                existing_by_name = storage.get_hypervisor_job_by_name(job_name)
                                if existing_by_name:
                                    imported["skipped_jobs"] += 1
                                    continue

                                # Check if the hypervisor exists
                                hv = storage.get_hypervisor(job_hv_id)
                                if not hv:
                                    hv_name = job_data.get("hypervisor_name")
                                    hv_host = job_data.get("hypervisor_host")

                                    all_hvs = storage.list_hypervisors()
                                    for existing_hv in all_hvs:
                                        if existing_hv["name"] == hv_name or existing_hv["host"] == hv_host:
                                            job_hv_id = existing_hv["id"]
                                            hv = existing_hv
                                            break

                                    if not hv:
                                        errors.append(f"Hypervisor not found for job '{job_name}'")
                                        continue

                                # guest_ids from metadata is already a list, pass it directly
                                # (add_hypervisor_job handles JSON encoding internally)
                                guest_ids = job_data.get("guest_ids") or []

                                storage.add_hypervisor_job(
                                    job_id=job_id,
                                    name=job_name,
                                    hypervisor_id=job_hv_id,
                                    guest_ids=guest_ids,
                                    repository_id=repo_id,
                                    backup_mode=job_data.get("backup_mode", "snapshot"),
                                    compression=job_data.get("compression", "zstd"),
                                    schedule_cron=job_data.get("schedule_cron"),
                                    enabled=job_data.get("enabled", True),
                                    copies_to_keep=job_data.get("copies_to_keep", 0),
                                )
                                imported["jobs"] += 1
                                logger.info(f"Imported hypervisor job '{job_name}' from repository")

                            except json_module.JSONDecodeError as e:
                                errors.append(f"Invalid JSON in {job_file.name}: {e}")
                            except Exception as e:
                                errors.append(f"Error importing {job_file.name}: {e}")

                    return {
                        "success": True,
                        "repository_id": repo_id,
                        "repository_name": repo.get("name"),
                        "imported": imported,
                        "errors": errors if errors else None,
                    }

                finally:
                    if mounted:
                        try:
                            subprocess.run(["sudo", "-n", "umount", "-l", mount_point], capture_output=True, timeout=30)
                        except Exception:
                            pass
                    try:
                        Path(mount_point).rmdir()
                    except Exception:
                        pass

            elif repo_type == "local":
                # LOCAL repo: read directly from filesystem
                local_path = share or repo.get("path", "")
                if not local_path:
                    return {
                        "success": False,
                        "error": "No local path configured for LOCAL repository",
                        "repository_id": repo_id,
                    }

                hypervisors_dir = Path(local_path) / "Hypervisors"
                if not hypervisors_dir.exists():
                    return {
                        "success": False,
                        "error": "No Hypervisors directory found in repository",
                        "repository_id": repo_id,
                    }

                for hv_dir in hypervisors_dir.iterdir():
                    if not hv_dir.is_dir() or hv_dir.name.startswith("."):
                        continue

                    jobs_dir = hv_dir / ".backer" / "hypervisor_jobs"
                    if not jobs_dir.exists():
                        continue

                    for job_file in jobs_dir.glob("*.json"):
                        try:
                            job_data = json_module.loads(job_file.read_text())
                            job_id = job_data.get("job_id")
                            job_name = job_data.get("name")
                            job_hv_id = job_data.get("hypervisor_id")

                            if not job_id or not job_name:
                                continue

                            # Filter by hypervisor_id if specified
                            if hypervisor_id and job_hv_id != hypervisor_id:
                                continue

                            # Check if job already exists
                            existing = storage.get_hypervisor_job(job_id)
                            if existing:
                                imported["skipped_jobs"] += 1
                                continue

                            existing_by_name = storage.get_hypervisor_job_by_name(job_name)
                            if existing_by_name:
                                imported["skipped_jobs"] += 1
                                continue

                            # Check if the hypervisor exists
                            hv = storage.get_hypervisor(job_hv_id)
                            if not hv:
                                hv_name = job_data.get("hypervisor_name")
                                hv_host = job_data.get("hypervisor_host")

                                all_hvs = storage.list_hypervisors()
                                for existing_hv in all_hvs:
                                    if existing_hv["name"] == hv_name or existing_hv["host"] == hv_host:
                                        job_hv_id = existing_hv["id"]
                                        hv = existing_hv
                                        break

                                if not hv:
                                    errors.append(f"Hypervisor not found for job '{job_name}'")
                                    continue

                            guest_ids = job_data.get("guest_ids") or []

                            storage.add_hypervisor_job(
                                job_id=job_id,
                                name=job_name,
                                hypervisor_id=job_hv_id,
                                guest_ids=guest_ids,
                                repository_id=repo_id,
                                backup_mode=job_data.get("backup_mode", "snapshot"),
                                compression=job_data.get("compression", "zstd"),
                                schedule_cron=job_data.get("schedule_cron"),
                                enabled=job_data.get("enabled", True),
                                copies_to_keep=job_data.get("copies_to_keep", 0),
                            )
                            imported["jobs"] += 1
                            logger.info(f"Imported hypervisor job '{job_name}' from LOCAL repository")

                        except json_module.JSONDecodeError as e:
                            errors.append(f"Invalid JSON in {job_file.name}: {e}")
                        except Exception as e:
                            errors.append(f"Error importing {job_file.name}: {e}")

                return {
                    "success": True,
                    "repository_id": repo_id,
                    "repository_name": repo.get("name"),
                    "imported": imported,
                    "errors": errors if errors else None,
                }

            else:
                return {
                    "success": False,
                    "error": f"Unsupported repository type: {repo_type}",
                    "repository_id": repo_id,
                }

        except Exception as e:
            logger.error(f"Failed to import hypervisor jobs from repository {repo_id}: {e}")
            return {
                "success": False,
                "error": str(e),
                "repository_id": repo_id,
            }

    @app.get("/api/v1/repositories/{repo_id}/discover-orphaned-backups")
    async def discover_orphaned_backups(
        repo_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Discover hypervisor backups not linked to active hypervisors.

        Scans the repository for VM/container backups whose hypervisor_id
        no longer exists in the database. This enables disaster recovery
        by identifying orphaned backups that can be adopted.

        Args:
            repo_id: Repository ID to scan

        Returns:
            {
                "orphaned_guests": [list of orphaned VM metadata],
                "orphaned_jobs": [list of orphaned job configs],
                "summary": {stats}
            }
        """
        from backer.server.hypervisor_discovery import HypervisorDiscoveryService

        repo = storage.get_repository(repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found")

        repo_type = repo.get("repo_type", "smb")

        # Only SMB/NFS repositories support hypervisor backups
        if repo_type not in ("smb", "nfs", "local"):
            raise HTTPException(
                status_code=400, detail=f"Repository type '{repo_type}' does not support hypervisor backups"
            )

        try:
            # Get repository credentials and paths
            mount_point = repo.get("mount_point")
            subpath = repo.get("path", "")
            server = repo.get("server", "")
            share = repo.get("share") or repo.get("export", "")
            username = repo.get("username")
            password = storage.get_storage_password(repo_id)
            domain = repo.get("domain")

            # Determine repo_path based on type and platform
            if mount_point:
                # Repository is mounted, use the mount point
                repo_path = mount_point
                if subpath:
                    repo_path = repo_path.rstrip("/") + "/" + subpath
            elif repo_type == "local":
                repo_path = repo.get("share") or repo.get("path", "")
                if not repo_path:
                    raise HTTPException(status_code=400, detail="Local repository path not configured")
            elif repo_type in ("smb", "nfs"):
                # For SMB/NFS, use subpath only (SMB client will be used on Linux)
                if sys.platform == "win32":
                    # Windows can use UNC paths
                    repo_path = f"\\\\{server}\\{share}"
                    if subpath:
                        subpath_windows = subpath.replace("/", "\\")
                        repo_path = f"{repo_path}\\{subpath_windows}"
                else:
                    # Linux will use SMB client - just pass subpath
                    repo_path = subpath or "."
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported repository type: {repo_type}")

            # Initialize discovery service with SMB credentials
            discovery = HypervisorDiscoveryService(
                repo_path=repo_path,
                repo_type=repo_type,
                storage=storage,
                server=server,
                share=share,
                username=username,
                password=password,
                domain=domain,
            )

            # Discover orphaned backups
            result = discovery.discover_orphaned_backups()

            return {"success": True, "repository_id": repo_id, **result}

        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"Failed to discover orphaned backups in repository {repo_id}")
            raise HTTPException(status_code=500, detail=f"Discovery failed: {str(e)}")

    @app.post("/api/v1/hypervisors/{hypervisor_id}/adopt-backups")
    async def adopt_orphaned_backups(
        hypervisor_id: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Adopt orphaned backups from repository to this hypervisor.

        Links existing VM/container backups to a new hypervisor, enabling
        disaster recovery. Updates metadata files to associate backups with
        the new hypervisor and optionally imports job configurations.

        Request body:
        {
            "repository_id": "repo-uuid",
            "guest_vmids": [100, 101, 102],  // Can be int or str (Hyper-V)
            "import_jobs": true
        }

        Args:
            hypervisor_id: Hypervisor ID to adopt backups to
        """
        from backer.server.hypervisor_discovery import HypervisorDiscoveryService

        # Parse request body
        body = await request.json()
        repository_id = body.get("repository_id")
        guest_vmids = body.get("guest_vmids", [])
        import_jobs = body.get("import_jobs", True)

        if not repository_id:
            raise HTTPException(status_code=400, detail="repository_id is required")

        if not guest_vmids:
            raise HTTPException(status_code=400, detail="guest_vmids cannot be empty")

        # Validate hypervisor exists
        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        # Validate repository exists
        repo = storage.get_repository(repository_id)
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found")

        repo_type = repo.get("repo_type", "smb")

        # Only SMB/NFS/local repositories support hypervisor backups
        if repo_type not in ("smb", "nfs", "local"):
            raise HTTPException(
                status_code=400, detail=f"Repository type '{repo_type}' does not support hypervisor backups"
            )

        try:
            # Get repository credentials and paths (same as discovery endpoint)
            mount_point = repo.get("mount_point")
            subpath = repo.get("path", "")
            server = repo.get("server", "")
            share = repo.get("share") or repo.get("export", "")
            username = repo.get("username")
            password = storage.get_storage_password(repository_id)
            domain = repo.get("domain")

            # Determine repo_path based on type and platform
            if mount_point:
                # Repository is mounted, use the mount point
                repo_path = mount_point
                if subpath:
                    repo_path = repo_path.rstrip("/") + "/" + subpath
            elif repo_type == "local":
                repo_path = repo.get("share") or repo.get("path", "")
                if not repo_path:
                    raise HTTPException(status_code=400, detail="Local repository path not configured")
            elif repo_type in ("smb", "nfs"):
                # For SMB/NFS, use subpath only (SMB client will be used on Linux)
                if sys.platform == "win32":
                    # Windows can use UNC paths
                    repo_path = f"\\\\{server}\\{share}"
                    if subpath:
                        subpath_windows = subpath.replace("/", "\\")
                        repo_path = f"{repo_path}\\{subpath_windows}"
                else:
                    # Linux will use SMB client - just pass subpath
                    repo_path = subpath or "."
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported repository type: {repo_type}")

            # Initialize discovery service with SMB credentials
            discovery = HypervisorDiscoveryService(
                repo_path=repo_path,
                repo_type=repo_type,
                storage=storage,
                server=server,
                share=share,
                username=username,
                password=password,
                domain=domain,
            )

            # Adopt backups
            result = discovery.adopt_backups(
                new_hypervisor_id=hypervisor_id,
                new_hypervisor_name=hypervisor["name"],
                guest_vmids=guest_vmids,
                import_jobs=import_jobs,
                repository_id=repository_id,
            )

            logger.info(
                f"Adopted {result['adopted_guests']} guests to hypervisor '{hypervisor['name']}' "
                f"({hypervisor_id}) from repository {repository_id}"
            )

            return {"success": True, "hypervisor_id": hypervisor_id, "repository_id": repository_id, **result}

        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"Failed to adopt backups to hypervisor {hypervisor_id}")
            raise HTTPException(status_code=500, detail=f"Adoption failed: {str(e)}")

    @app.post("/api/v1/repositories/{repo_id}/wipe")
    def wipe_repository(
        repo_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Wipe all contents of a repository.

        This is a DESTRUCTIVE operation that recursively deletes ALL files
        and directories within the repository path. Use with extreme caution.
        """
        import subprocess

        from backer.server.repositories import SMBBrowser, smb_auth_file

        repo = storage.get_repository(repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found")

        repo_type = repo.get("repo_type", "smb")
        subpath = repo.get("path", "")
        server = repo.get("server", "")
        share = repo.get("share", "")
        username = repo.get("username")
        password = storage.get_storage_password(repo_id)
        domain = repo.get("domain")
        repo_name = repo.get("name", repo_id)

        def wipe_smb_recursive(task: Task) -> dict[str, Any]:
            """Recursively wipe all contents from an SMB share path."""
            deleted_items = 0
            errors: list[str] = []

            def run_smb_command(commands: str, timeout: int = 30) -> tuple[int, str, str]:
                """Run smbclient with given commands."""
                with smb_auth_file(username, password, domain) as auth_path:
                    cmd = ["smbclient", f"//{server}/{share}", "-t", "10"]

                    if auth_path:
                        cmd.extend(["-A", auth_path])
                    else:
                        cmd.append("-N")

                    cmd.extend(["-c", commands])

                    try:
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
                        return result.returncode, result.stdout, result.stderr
                    except subprocess.TimeoutExpired:
                        return -1, "", "Command timed out"
                    except Exception as e:
                        return -1, "", str(e)

            def delete_path_recursive(path: str, depth: int = 0) -> int:
                """Recursively delete a path. Returns count of deleted items."""
                nonlocal errors
                if depth > 50:  # Prevent infinite recursion
                    errors.append(f"Max depth exceeded at {path}")
                    return 0

                count = 0
                display_path = path if path else "(root)"
                task.message = f"Scanning {display_path}..."

                # List contents using SMBBrowser.list_directory to get is_dir attribute
                success, entries = SMBBrowser.list_directory(server, share, path, username, password, domain)
                if not success:
                    # Log the error - path might not exist or be inaccessible
                    error_msg = entries if isinstance(entries, str) else "Unknown error"
                    logger.warning(f"Failed to list directory '{display_path}': {error_msg}")
                    if depth == 0:
                        errors.append(f"Failed to list root directory: {error_msg}")
                    return 0

                logger.info(f"Found {len(entries)} entries in '{display_path}'")

                # Process each entry - entries are DirectoryEntry objects with is_dir attribute
                for entry in entries:
                    if entry.name in [".", ".."]:
                        continue

                    entry_path = f"{path}/{entry.name}" if path else entry.name

                    if entry.is_dir:
                        # It's a directory, recurse first to delete contents
                        count += delete_path_recursive(entry_path, depth + 1)

                        # Then delete the empty directory - use cd + rmdir for paths with spaces
                        task.message = f"Removing directory {entry_path}..."
                        if path:
                            cmd = f'cd "{path}"; rmdir "{entry.name}"'
                        else:
                            cmd = f'rmdir "{entry.name}"'
                        rc, _, err = run_smb_command(cmd)
                        if rc == 0 or "NT_STATUS_NO_SUCH_FILE" in err or "NT_STATUS_OBJECT_NAME_NOT_FOUND" in err:
                            logger.info(f"Deleted directory: {entry_path}")
                            count += 1
                        elif "NT_STATUS_DIRECTORY_NOT_EMPTY" in err:
                            # Directory not empty - try to re-scan and delete remaining items
                            logger.warning(f"Directory not empty, re-scanning: {entry_path}")
                            count += delete_path_recursive(entry_path, depth + 1)
                            # Try again to delete the directory
                            rc2, _, err2 = run_smb_command(cmd)
                            not_found = "NT_STATUS_NO_SUCH_FILE" in err2 or "NT_STATUS_OBJECT_NAME_NOT_FOUND" in err2
                            if rc2 == 0 or not_found:
                                logger.info(f"Deleted directory on retry: {entry_path}")
                                count += 1
                            else:
                                errors.append(f"rmdir {entry_path}: still not empty after retry")
                        else:
                            errors.append(f"rmdir {entry_path}: {err.strip()[:100]}")
                    else:
                        # It's a file - use cd + del for paths with spaces
                        task.message = f"Deleting {entry_path}..."
                        if path:
                            cmd = f'cd "{path}"; del "{entry.name}"'
                        else:
                            cmd = f'del "{entry.name}"'
                        rc, _, err = run_smb_command(cmd)
                        if rc == 0 or "NT_STATUS_NO_SUCH_FILE" in err or "NT_STATUS_OBJECT_NAME_NOT_FOUND" in err:
                            count += 1
                        else:
                            errors.append(f"del {entry_path}: {err.strip()[:100]}")

                return count

            try:
                # Start from the repository's subpath
                base_path = subpath.strip("/") if subpath else ""
                logger.info(
                    f"Starting SMB wipe for repo {repo_id}: server={server}, share={share}, base_path='{base_path}'"
                )
                deleted_items = delete_path_recursive(base_path)

                if errors:
                    logger.warning(f"Wipe completed with {len(errors)} errors: {errors[:5]}")

                return {
                    "success": True,
                    "deleted_items": deleted_items,
                    "errors": errors[:10] if errors else [],
                    "repository_id": repo_id,
                }

            except Exception as e:
                logger.exception(f"Error during wipe: {e}")
                return {
                    "success": False,
                    "error": str(e),
                    "deleted_items": deleted_items,
                    "repository_id": repo_id,
                }

        def wipe_nfs_recursive(task: Task) -> dict[str, Any]:
            """Recursively wipe all contents from an NFS share path."""
            import tempfile

            deleted_items = 0
            errors: list[str] = []
            mount_point = None
            mounted = False

            try:
                # Create temp mount point
                mount_point = tempfile.mkdtemp(prefix="backer_nfs_wipe_")

                # Build NFS source
                nfs_export = share or repo.get("path", "")
                nfs_source = f"{server}:{nfs_export}"

                task.message = f"Mounting NFS share {nfs_source}..."

                # Mount the NFS share with read-write access
                mount_cmd = [
                    "sudo",
                    "-n",
                    "mount",
                    "-t",
                    "nfs",
                    "-o",
                    "soft,timeo=50,retrans=2,rw",
                    nfs_source,
                    mount_point,
                ]
                result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=60)
                if result.returncode != 0:
                    return {
                        "success": False,
                        "error": f"Failed to mount NFS: {result.stderr.strip()}",
                        "repository_id": repo_id,
                    }

                mounted = True

                # Determine base path within the mount
                if subpath:
                    base_path = Path(mount_point) / subpath.strip("/")
                else:
                    base_path = Path(mount_point)

                if not base_path.exists():
                    return {
                        "success": True,
                        "deleted_items": 0,
                        "errors": [],
                        "repository_id": repo_id,
                        "message": "Path does not exist, nothing to wipe",
                    }

                task.message = "Scanning directory contents..."

                # Count items first for progress reporting
                def count_items(path: Path) -> int:
                    count = 0
                    try:
                        for item in path.iterdir():
                            count += 1
                            if item.is_dir() and not item.is_symlink():
                                count += count_items(item)
                    except PermissionError:
                        pass
                    return count

                total_items = count_items(base_path)
                processed = 0

                def delete_contents(path: Path, depth: int = 0) -> int:
                    """Recursively delete contents of a directory."""
                    nonlocal errors, processed
                    if depth > 50:
                        errors.append(f"Max depth exceeded at {path}")
                        return 0

                    count = 0
                    try:
                        for item in list(path.iterdir()):
                            processed += 1
                            if total_items > 0:
                                task.progress = min(int((processed / total_items) * 80) + 10, 90)
                                task.message = f"Deleting {item.name}..."

                            try:
                                if item.is_symlink():
                                    item.unlink()
                                    count += 1
                                elif item.is_dir():
                                    # Recursively delete directory contents first
                                    count += delete_contents(item, depth + 1)
                                    # Then remove the empty directory
                                    item.rmdir()
                                    count += 1
                                else:
                                    item.unlink()
                                    count += 1
                            except PermissionError:
                                # Try with sudo for stubborn files
                                try:
                                    if item.is_dir():
                                        subprocess.run(
                                            ["sudo", "-n", "rm", "-rf", str(item)], capture_output=True, timeout=30
                                        )
                                    else:
                                        subprocess.run(
                                            ["sudo", "-n", "rm", "-f", str(item)], capture_output=True, timeout=30
                                        )
                                    count += 1
                                except Exception:
                                    errors.append(f"Permission denied: {item}")
                            except Exception as e:
                                errors.append(f"Error deleting {item}: {str(e)[:50]}")
                    except PermissionError:
                        errors.append(f"Cannot access directory: {path}")
                    except Exception as e:
                        errors.append(f"Error scanning {path}: {str(e)[:50]}")

                    return count

                # Delete contents (but not the base directory itself)
                deleted_items = delete_contents(base_path)

                if errors:
                    logger.warning(f"NFS wipe completed with {len(errors)} errors: {errors[:5]}")

                return {
                    "success": True,
                    "deleted_items": deleted_items,
                    "errors": errors[:10] if errors else [],
                    "repository_id": repo_id,
                }

            except Exception as e:
                logger.exception(f"Error during NFS wipe: {e}")
                return {
                    "success": False,
                    "error": str(e),
                    "deleted_items": deleted_items,
                    "repository_id": repo_id,
                }

            finally:
                # Unmount
                if mounted and mount_point:
                    task.message = "Unmounting NFS share..."
                    try:
                        subprocess.run(["sudo", "-n", "umount", mount_point], capture_output=True, timeout=30)
                    except Exception:
                        try:
                            subprocess.run(["sudo", "-n", "umount", "-l", mount_point], capture_output=True, timeout=10)
                        except Exception:
                            pass

                # Remove temp mount point
                if mount_point:
                    try:
                        Path(mount_point).rmdir()
                    except Exception:
                        pass

        def run_wipe(task: Task) -> dict[str, Any]:
            task.message = "Wiping repository contents..."

            if repo_type == "smb":
                return wipe_smb_recursive(task)
            elif repo_type == "nfs":
                return wipe_nfs_recursive(task)
            else:
                return {
                    "success": False,
                    "error": f"Wipe not supported for repository type: {repo_type}",
                    "repository_id": repo_id,
                }

        task_manager = get_task_manager()
        task = task_manager.submit(
            task_type="wipe_repository",
            description=f"Wiping all contents from repository '{repo_name}'",
            func=run_wipe,
        )

        return {"task_id": task.id, "status": "wiping", "message": "Repository wipe started"}

    # ============ Hypervisor Management ============

    @app.get("/api/v1/hypervisors")
    def list_hypervisors(
        hypervisor_type: str | None = None,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List all hypervisors."""
        return storage.list_hypervisors(hypervisor_type)

    @app.post("/api/v1/hypervisors")
    async def create_hypervisor(
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Add a new hypervisor."""
        # Get request body
        body = await request.json()

        name = body.get("name", "").strip()
        hypervisor_type = body.get("hypervisor_type", "proxmox")
        host = body.get("host", "").strip()
        port = body.get("port", 8006)
        auth_method = body.get("auth_method", "token")
        username = body.get("username")
        token_id = body.get("token_id")
        token_secret = body.get("token_secret")
        password = body.get("password")
        verify_ssl = body.get("verify_ssl", False)

        # SSH settings for incremental backups
        ssh_user = body.get("ssh_user", "root")
        ssh_port = body.get("ssh_port", 22)
        ssh_key_path = body.get("ssh_key_path")
        ssh_use_api_password = body.get("ssh_use_api_password", True)

        # Hyper-V specific: domain for WinRM authentication
        domain = body.get("domain")
        # Hyper-V Cluster specific: cluster name and permission status
        cluster_name = body.get("cluster_name")
        permissions_configured = body.get("permissions_configured", False)

        # Build config dict for type-specific settings
        config: dict[str, Any] = {}
        if domain:
            config["domain"] = domain
        if cluster_name:
            config["cluster_name"] = cluster_name
        if hypervisor_type == "hyperv-cluster":
            config["permissions_configured"] = permissions_configured

        # Validate
        validate_name(name, "name")
        if not host:
            raise HTTPException(status_code=400, detail="Host is required")

        if auth_method == "token" and (not token_id or not token_secret):
            raise HTTPException(status_code=400, detail="Token ID and secret required for token auth")
        if auth_method == "password" and (not username or not password):
            raise HTTPException(status_code=400, detail="Username and password required for password auth")

        # Check for duplicate name
        if storage.get_hypervisor_by_name(name):
            raise HTTPException(status_code=400, detail="Hypervisor with this name already exists")

        hypervisor_id = str(uuid4())

        storage.add_hypervisor(
            hypervisor_id=hypervisor_id,
            name=name,
            hypervisor_type=hypervisor_type,
            host=host,
            port=port,
            auth_method=auth_method,
            username=username,
            token_id=token_id,
            token_secret=token_secret,
            password=password,
            verify_ssl=verify_ssl,
            config=config if config else None,
            ssh_user=ssh_user,
            ssh_port=ssh_port,
            ssh_key_path=ssh_key_path,
            ssh_use_api_password=ssh_use_api_password,
        )

        return {"id": hypervisor_id, "name": name, "status": "created"}

    @app.post("/api/v1/hypervisors/test")
    async def test_hypervisor_credentials(
        request: Request,
    ) -> dict[str, Any]:
        """Test hypervisor connection without saving.

        This is used to validate credentials before creating a hypervisor.
        """
        from backer.hypervisors.hyperv import HyperVAPI
        from backer.hypervisors.proxmox import ProxmoxAPI, ProxmoxAuthMethod
        from backer.hypervisors.unraid import UnraidAPI

        body = await request.json()

        hypervisor_type = body.get("hypervisor_type", "proxmox")
        host = body.get("host", "").strip()
        port = body.get("port", 8006)
        auth_method = body.get("auth_method", "token")
        username = body.get("username")
        token_id = body.get("token_id")
        token_secret = body.get("token_secret")
        password = body.get("password")
        verify_ssl = body.get("verify_ssl", False)
        domain = body.get("domain")

        if not host:
            return {"success": False, "message": "Host is required"}

        if hypervisor_type not in ("proxmox", "unraid", "hyperv", "hyperv-cluster"):
            return {"success": False, "message": f"Unsupported hypervisor type: {hypervisor_type}"}

        try:
            if hypervisor_type == "unraid":
                # Unraid uses API key authentication
                # The API key is passed as token_secret for consistency with the form
                api_key = token_secret or password
                if not api_key:
                    return {"success": False, "message": "API key is required for Unraid"}

                # Default port for Unraid is 443 (HTTPS)
                if port == 8006:  # User didn't change from Proxmox default
                    port = 443

                api = UnraidAPI(
                    host=host,
                    api_key=api_key,
                    port=port,
                    use_https=port == 443 or port == 8443,
                    verify_ssl=verify_ssl,
                )

                success, message = api.test_connection()

                return {
                    "success": success,
                    "message": message,
                    "version": api.version if success else None,
                }

            elif hypervisor_type in ("hyperv", "hyperv-cluster"):
                # Hyper-V uses WinRM + PowerShell
                if not username:
                    return {"success": False, "message": "Username is required for Hyper-V"}
                if not password:
                    return {"success": False, "message": "Password is required for Hyper-V"}

                # Default port for WinRM is 5985 (HTTP) or 5986 (HTTPS)
                if port == 8006:  # User didn't change from Proxmox default
                    port = 5985

                if hypervisor_type == "hyperv-cluster":
                    from backer.hypervisors.hyperv import HyperVClusterAPI

                    cluster_name = body.get("cluster_name")
                    api = HyperVClusterAPI(
                        host=host,
                        username=username,
                        password=password,
                        cluster_name=cluster_name,
                        port=port,
                        use_ssl=port == 5986,
                        verify_ssl=verify_ssl,
                        domain=domain,
                    )
                else:
                    api = HyperVAPI(
                        host=host,
                        username=username,
                        password=password,
                        port=port,
                        use_ssl=port == 5986,
                        verify_ssl=verify_ssl,
                        domain=domain,
                    )

                success, message = api.test_connection()

                return {
                    "success": success,
                    "message": message,
                    "version": api.version if success else None,
                }

            else:
                # Proxmox
                pve_auth = ProxmoxAuthMethod.TOKEN if auth_method == "token" else ProxmoxAuthMethod.PASSWORD

                api = ProxmoxAPI(
                    host=host,
                    port=port,
                    auth_method=pve_auth,
                    username=username,
                    token_id=token_id,
                    token_secret=token_secret,
                    password=password,
                    verify_ssl=verify_ssl,
                )

                success, message = api.test_connection()

                return {
                    "success": success,
                    "message": message,
                    "version": api.version if success else None,
                }

        except Exception as e:
            logger.exception("Failed to test hypervisor connection")
            return {"success": False, "message": str(e)}

    @app.get("/api/v1/hypervisors/{hypervisor_id}")
    def get_hypervisor(
        hypervisor_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Get hypervisor details."""
        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")
        return hypervisor

    @app.put("/api/v1/hypervisors/{hypervisor_id}")
    async def update_hypervisor(
        hypervisor_id: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Update a hypervisor."""
        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        body = await request.json()

        # Only update fields that are provided
        update_kwargs: dict[str, Any] = {}
        if "name" in body:
            validate_name(body["name"], "name")
            update_kwargs["name"] = body["name"]
        if "host" in body:
            update_kwargs["host"] = body["host"]
        if "port" in body:
            update_kwargs["port"] = body["port"]
        if "auth_method" in body:
            update_kwargs["auth_method"] = body["auth_method"]
        if "username" in body:
            update_kwargs["username"] = body["username"]
        if "token_id" in body:
            update_kwargs["token_id"] = body["token_id"]
        if "token_secret" in body:
            update_kwargs["token_secret"] = body["token_secret"]
        if "password" in body:
            update_kwargs["password"] = body["password"]
        if "verify_ssl" in body:
            update_kwargs["verify_ssl"] = body["verify_ssl"]

        storage.update_hypervisor(hypervisor_id, **update_kwargs)

        return {"id": hypervisor_id, "status": "updated"}

    @app.delete("/api/v1/hypervisors/{hypervisor_id}")
    def delete_hypervisor(
        hypervisor_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Delete a hypervisor and all associated data.

        This permanently deletes:
        - All backup jobs for this hypervisor
        - All backup files from repositories
        - All metadata from repositories
        - The hypervisor configuration

        After deletion, the repository will be clean with no trace of this hypervisor.
        The cleanup runs in the background to avoid blocking the API.
        """
        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        hypervisor_name = hypervisor.get("name", "Unknown")

        # Get all jobs for this hypervisor and collect repository data
        all_jobs = storage.list_hypervisor_jobs()
        hypervisor_jobs = [j for j in all_jobs if j.get("hypervisor_id") == hypervisor_id]

        logger.info(f"Deleting hypervisor '{hypervisor_name}' with {len(hypervisor_jobs)} jobs")

        # Collect all data needed for cleanup before deleting from database
        cleanup_data = []
        repos_to_cleanup = {}

        for job in hypervisor_jobs:
            repository_id = job.get("repository_id")
            if repository_id:
                repository = storage.get_repository(repository_id)
                if repository:
                    # Make copies to avoid threading issues
                    cleanup_data.append(
                        {
                            "job": dict(job),
                            "repository": dict(repository),
                            "repository_id": repository_id,
                        }
                    )
                    # Track unique repositories for folder cleanup
                    if repository_id not in repos_to_cleanup:
                        repos_to_cleanup[repository_id] = dict(repository)

        # Delete from database immediately (this also deletes jobs and runs)
        storage.delete_hypervisor(hypervisor_id)

        # Run cleanup in background if there's data to clean
        if cleanup_data or repos_to_cleanup:
            # Make a copy of hypervisor data for the thread
            hypervisor_copy = dict(hypervisor)

            def cleanup_hypervisor_data(task: Task) -> dict[str, Any]:
                """Background task to clean up hypervisor data from repositories.

                NOTE: We get a fresh Storage instance here to avoid SQLite threading issues.
                """
                import gc
                from pathlib import Path

                from backer.server.storage import Storage

                cleanup_errors = []
                jobs_cleaned = 0
                task.message = f"Cleaning up backup data for hypervisor '{hypervisor_name}'"
                task.progress = 10

                # Get a fresh storage instance for this thread
                db_path = Path.home() / ".backer" / "backer.db"
                thread_storage = Storage(db_path)

                # Clean up each job's backup data
                total_jobs = len(cleanup_data)
                for idx, data in enumerate(cleanup_data):
                    try:
                        job_name = data["job"].get("name", "Unknown")
                        task.message = f"Cleaning up job '{job_name}'"
                        task.progress = 10 + int(60 * (idx / max(total_jobs, 1)))

                        _cleanup_hypervisor_job_data(
                            repository=data["repository"],
                            hypervisor=hypervisor_copy,
                            job=data["job"],
                            storage=thread_storage,
                            repository_id=data["repository_id"],
                        )
                        jobs_cleaned += 1
                        logger.info(f"Cleaned up job '{job_name}' data from repository")
                    except Exception as e:
                        logger.warning(f"Failed to clean up job '{data['job'].get('name')}': {e}")
                        cleanup_errors.append(f"Job {data['job'].get('name')}: {e}")

                # Clean up hypervisor folders
                task.message = "Cleaning up hypervisor folders"
                task.progress = 70

                for repository_id, repository in repos_to_cleanup.items():
                    try:
                        _cleanup_hypervisor_folder(
                            repository=repository,
                            hypervisor=hypervisor_copy,
                            storage=thread_storage,
                            repository_id=repository_id,
                        )
                        logger.info(f"Cleaned up hypervisor folder from repository '{repository.get('name')}'")
                    except Exception as e:
                        logger.warning(f"Failed to clean up hypervisor folder: {e}")
                        cleanup_errors.append(f"Hypervisor folder cleanup: {e}")

                task.progress = 100

                # Explicitly close the thread's storage connection to release SQLite lock
                del thread_storage
                gc.collect()  # Force garbage collection to close database connection immediately

                result: dict[str, Any] = {
                    "id": hypervisor_id,
                    "status": "cleanup_complete",
                    "jobs_cleaned": jobs_cleaned,
                    "repositories_cleaned": len(repos_to_cleanup),
                    "success": len(cleanup_errors) == 0,
                }
                if cleanup_errors:
                    result["cleanup_warnings"] = cleanup_errors

                task.message = f"Cleanup complete for '{hypervisor_name}'"
                return result

            task_manager = get_task_manager()
            task = task_manager.submit(
                task_type="delete_hypervisor",
                description=f"Deleting backup data for hypervisor '{hypervisor_name}'",
                func=cleanup_hypervisor_data,
            )

            return {
                "id": hypervisor_id,
                "status": "deleted",
                "cleanup_task_id": task.id,
                "message": "Hypervisor deleted. Cleanup running in background.",
            }

        # No repository data to clean up
        return {"id": hypervisor_id, "status": "deleted"}

    @app.post("/api/v1/hypervisors/{hypervisor_id}/test")
    def test_hypervisor_connection(
        hypervisor_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Test connection to a hypervisor."""
        from backer.hypervisors.hyperv import HyperVAPI
        from backer.hypervisors.proxmox import ProxmoxAPI, ProxmoxAuthMethod
        from backer.hypervisors.unraid import UnraidAPI

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        hypervisor_type = hypervisor.get("hypervisor_type", "proxmox")

        # Get credentials
        token_secret = storage.get_hypervisor_token_secret(hypervisor_id)
        password = storage.get_hypervisor_password(hypervisor_id)

        if hypervisor_type == "unraid":
            # Unraid uses API key authentication (stored as token_secret)
            api_key = token_secret or password
            if not api_key:
                return {"success": False, "message": "API key not configured", "hypervisor_id": hypervisor_id}

            port = hypervisor.get("port", 443)
            api = UnraidAPI(
                host=hypervisor["host"],
                api_key=api_key,
                port=port,
                use_https=port in (443, 8443),
                verify_ssl=hypervisor.get("verify_ssl", False),
            )

            success, message = api.test_connection()

        elif hypervisor_type in ("hyperv", "hyperv-cluster"):
            # Hyper-V uses WinRM + PowerShell
            if not password:
                return {"success": False, "message": "Password not configured", "hypervisor_id": hypervisor_id}

            port = hypervisor.get("port", 5985)
            # Get domain from hypervisor data or config
            domain = hypervisor.get("domain") or hypervisor.get("config", {}).get("domain")

            if hypervisor_type == "hyperv-cluster":
                from backer.hypervisors.hyperv import HyperVClusterAPI

                cluster_name = hypervisor.get("cluster_name") or hypervisor.get("config", {}).get("cluster_name")
                api = HyperVClusterAPI(
                    host=hypervisor["host"],
                    username=hypervisor.get("username", "Administrator"),
                    password=password,
                    cluster_name=cluster_name,
                    port=port,
                    use_ssl=port == 5986,
                    verify_ssl=hypervisor.get("verify_ssl", False),
                    domain=domain,
                )
            else:
                api = HyperVAPI(
                    host=hypervisor["host"],
                    username=hypervisor.get("username", "Administrator"),
                    password=password,
                    port=port,
                    use_ssl=port == 5986,
                    verify_ssl=hypervisor.get("verify_ssl", False),
                    domain=domain,
                )

            success, message = api.test_connection()

        elif hypervisor_type == "proxmox":
            auth_method = (
                ProxmoxAuthMethod.TOKEN if hypervisor["auth_method"] == "token" else ProxmoxAuthMethod.PASSWORD
            )

            api = ProxmoxAPI(
                host=hypervisor["host"],
                port=hypervisor.get("port", 8006),
                token_id=hypervisor.get("token_id"),
                token_secret=token_secret,
                username=hypervisor.get("username"),
                password=password,
                auth_method=auth_method,
                verify_ssl=hypervisor.get("verify_ssl", False),
            )

            success, message = api.test_connection()

        else:
            return {
                "success": False,
                "message": f"Unsupported hypervisor type: {hypervisor_type}",
                "hypervisor_id": hypervisor_id,
            }

        # Update hypervisor status
        if success:
            storage.update_hypervisor(
                hypervisor_id,
                status="connected",
                version=message,
            )
        else:
            storage.update_hypervisor(hypervisor_id, status="error")

        return {
            "success": success,
            "message": message,
            "hypervisor_id": hypervisor_id,
        }

    @app.get("/api/v1/hypervisors/{hypervisor_id}/guests")
    def list_hypervisor_guests(
        hypervisor_id: str,
        node: str | None = None,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List VMs and containers on a hypervisor."""
        from backer.hypervisors.hyperv import HyperVAPI, HyperVBackupManager
        from backer.hypervisors.proxmox import ProxmoxAPI, ProxmoxAuthMethod
        from backer.hypervisors.unraid import UnraidAPI, UnraidBackupManager

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        hypervisor_type = hypervisor.get("hypervisor_type", "proxmox")
        token_secret = storage.get_hypervisor_token_secret(hypervisor_id)
        password = storage.get_hypervisor_password(hypervisor_id)

        try:
            if hypervisor_type == "unraid":
                # Unraid uses API key authentication (stored as token_secret)
                api_key = token_secret or password
                if not api_key:
                    raise HTTPException(status_code=400, detail="API key not configured")

                port = hypervisor.get("port", 443)
                api = UnraidAPI(
                    host=hypervisor["host"],
                    api_key=api_key,
                    port=port,
                    use_https=port in (443, 8443),
                    verify_ssl=hypervisor.get("verify_ssl", False),
                )

                # Create backup manager to get unified guest list
                backup_manager = UnraidBackupManager(
                    api=api,
                    ssh_host=hypervisor["host"],
                    ssh_user=hypervisor.get("ssh_user", "root"),
                    ssh_port=hypervisor.get("ssh_port", 22),
                    ssh_key_path=hypervisor.get("ssh_key_path"),
                    ssh_password=password if hypervisor.get("ssh_use_api_password") else None,
                )

                return backup_manager.list_all_guests()

            elif hypervisor_type in ("hyperv", "hyperv-cluster"):
                # Hyper-V uses WinRM + PowerShell
                if not password:
                    raise HTTPException(status_code=400, detail="Password not configured")

                port = hypervisor.get("port", 5985)
                # Get domain from hypervisor data or config
                domain = hypervisor.get("domain") or hypervisor.get("config", {}).get("domain")

                if hypervisor_type == "hyperv-cluster":
                    from backer.hypervisors.hyperv import HyperVClusterAPI, HyperVClusterBackupManager

                    cluster_name = hypervisor.get("cluster_name") or hypervisor.get("config", {}).get("cluster_name")
                    api = HyperVClusterAPI(
                        host=hypervisor["host"],
                        username=hypervisor.get("username", "Administrator"),
                        password=password,
                        cluster_name=cluster_name,
                        port=port,
                        use_ssl=port == 5986,
                        verify_ssl=hypervisor.get("verify_ssl", False),
                        domain=domain,
                    )
                    backup_manager = HyperVClusterBackupManager(api)
                else:
                    api = HyperVAPI(
                        host=hypervisor["host"],
                        username=hypervisor.get("username", "Administrator"),
                        password=password,
                        port=port,
                        use_ssl=port == 5986,
                        verify_ssl=hypervisor.get("verify_ssl", False),
                        domain=domain,
                    )
                    backup_manager = HyperVBackupManager(api)

                return backup_manager.list_all_guests()

            elif hypervisor_type == "proxmox":
                auth_method = (
                    ProxmoxAuthMethod.TOKEN if hypervisor["auth_method"] == "token" else ProxmoxAuthMethod.PASSWORD
                )

                api = ProxmoxAPI(
                    host=hypervisor["host"],
                    port=hypervisor.get("port", 8006),
                    token_id=hypervisor.get("token_id"),
                    token_secret=token_secret,
                    username=hypervisor.get("username"),
                    password=password,
                    auth_method=auth_method,
                    verify_ssl=hypervisor.get("verify_ssl", False),
                )

                # Authenticate if using password-based auth
                if auth_method == ProxmoxAuthMethod.PASSWORD:
                    api.authenticate()

                guests = api.list_guests(node)
                return [
                    {
                        "vmid": g.vmid,
                        "name": g.name,
                        "node": g.node,
                        "type": g.guest_type.value,
                        "status": g.status,
                        "cpus": g.cpus,
                        "maxmem_gb": round(g.maxmem_gb, 2),
                        "maxdisk_gb": round(g.maxdisk_gb, 2),
                        "template": g.template,
                        "tags": g.tags,
                    }
                    for g in guests
                ]
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported hypervisor type: {hypervisor_type}")

        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"Failed to list guests for hypervisor {hypervisor_id}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/v1/hypervisors/{hypervisor_id}/nodes")
    def list_hypervisor_nodes(
        hypervisor_id: str,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List nodes on a hypervisor cluster."""
        from backer.hypervisors.proxmox import ProxmoxAPI, ProxmoxAuthMethod

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        token_secret = storage.get_hypervisor_token_secret(hypervisor_id)
        password = storage.get_hypervisor_password(hypervisor_id)

        auth_method = ProxmoxAuthMethod.TOKEN if hypervisor["auth_method"] == "token" else ProxmoxAuthMethod.PASSWORD

        api = ProxmoxAPI(
            host=hypervisor["host"],
            port=hypervisor["port"],
            token_id=hypervisor.get("token_id"),
            token_secret=token_secret,
            username=hypervisor.get("username"),
            password=password,
            auth_method=auth_method,
            verify_ssl=hypervisor.get("verify_ssl", False),
        )

        try:
            # Authenticate if using password-based auth
            if auth_method == ProxmoxAuthMethod.PASSWORD:
                api.authenticate()

            nodes = api.list_nodes()
            return [
                {
                    "node": n.node,
                    "status": n.status,
                    "cpu_percent": round(n.cpu, 1),
                    "maxcpu": n.maxcpu,
                    "mem_used_gb": round(n.mem / (1024**3), 2),
                    "mem_total_gb": round(n.maxmem / (1024**3), 2),
                    "uptime_hours": round(n.uptime / 3600, 1),
                }
                for n in nodes
            ]
        except Exception as e:
            logger.exception(f"Failed to list nodes for hypervisor {hypervisor_id}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/v1/hypervisors/{hypervisor_id}/cluster-nodes")
    def list_hyperv_cluster_nodes(
        hypervisor_id: str,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List nodes in a Hyper-V cluster.

        Returns a list of cluster nodes with their status for node selection
        during restore operations.
        """
        from backer.hypervisors.hyperv import HyperVClusterAPI

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        hypervisor_type = hypervisor.get("hypervisor_type")
        if hypervisor_type != "hyperv-cluster":
            raise HTTPException(status_code=400, detail="This endpoint is only for Hyper-V cluster hypervisors")

        password = storage.get_hypervisor_password(hypervisor_id)
        if not password:
            raise HTTPException(status_code=400, detail="Password not configured")

        port = hypervisor.get("port", 5985)
        domain = hypervisor.get("domain") or hypervisor.get("config", {}).get("domain")
        cluster_name = hypervisor.get("cluster_name") or hypervisor.get("config", {}).get("cluster_name")

        try:
            api = HyperVClusterAPI(
                host=hypervisor["host"],
                username=hypervisor.get("username", "Administrator"),
                password=password,
                cluster_name=cluster_name,
                port=port,
                use_ssl=port == 5986,
                verify_ssl=hypervisor.get("verify_ssl", False),
                domain=domain,
            )

            nodes = api.get_cluster_nodes()
            return nodes

        except Exception as e:
            logger.exception(f"Failed to list cluster nodes for hypervisor {hypervisor_id}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/v1/hypervisors/cluster/check-permissions")
    def check_cluster_permissions(
        request: dict[str, Any],
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Check cluster permissions for setup wizard.

        This endpoint is used during the cluster hypervisor setup wizard to verify
        that the user has the required permissions on all cluster nodes.

        Request body:
            host: Cluster name or IP
            username: Username
            password: Password
            domain: Domain (optional)
            cluster_name: Cluster name

        Returns:
            Permission status for each node and setup script
        """
        from backer.hypervisors.hyperv import HyperVClusterAPI

        host = request.get("host")
        username = request.get("username")
        password = request.get("password")
        domain = request.get("domain")
        cluster_name = request.get("cluster_name") or None  # Convert empty string to None
        port = request.get("port", 5985)

        if not all([host, username, password]):
            raise HTTPException(status_code=400, detail="Missing required fields: host, username, password")

        # If cluster_name is not provided, it will be auto-detected by HyperVClusterAPI
        try:
            api = HyperVClusterAPI(
                host=host,
                username=username,
                password=password,
                cluster_name=cluster_name,  # Can be None for auto-detection
                port=port,
                use_ssl=port == 5986,
                verify_ssl=False,
                domain=domain,
            )

            result = api.check_cluster_permissions()
            return result

        except Exception as e:
            logger.exception("Failed to check cluster permissions")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/v1/hypervisors/{hypervisor_id}/storages")
    def list_hypervisor_storages(
        hypervisor_id: str,
        node: str | None = None,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List backup-capable storages on a hypervisor."""
        from backer.hypervisors.proxmox import ProxmoxAPI, ProxmoxAuthMethod
        from backer.hypervisors.unraid import UnraidAPI

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        hypervisor_type = hypervisor.get("hypervisor_type", "proxmox")
        token_secret = storage.get_hypervisor_token_secret(hypervisor_id)
        password = storage.get_hypervisor_password(hypervisor_id)

        try:
            if hypervisor_type == "unraid":
                # Unraid uses API key authentication (stored as token_secret)
                api_key = token_secret or password
                if not api_key:
                    raise HTTPException(status_code=400, detail="API key not configured")

                port = hypervisor.get("port", 443)
                api = UnraidAPI(
                    host=hypervisor["host"],
                    api_key=api_key,
                    port=port,
                    use_https=port in (443, 8443),
                    verify_ssl=hypervisor.get("verify_ssl", False),
                )

                # Get array status as "storage" info
                array_status = api.get_array_status()
                storages = []

                # Add array as main storage
                capacity = array_status.get("capacity", {}).get("disks", {})
                storages.append(
                    {
                        "storage": "array",
                        "type": "array",
                        "node": "unraid",
                        "content": "images,backup",
                        "path": "/mnt/user",
                        "active": array_status.get("state") == "STARTED",
                        "enabled": True,
                        "shared": True,
                        "total": capacity.get("total", 0),
                        "used": capacity.get("used", 0),
                        "avail": capacity.get("free", 0),
                    }
                )

                # Add shares as storage locations when supported by the API
                if api.supports_shares:
                    shares = api.list_shares()
                    if api.supports_shares:
                        for share in shares:
                            storages.append(
                                {
                                    "storage": f"share:{share.name}",
                                    "type": "share",
                                    "node": "unraid",
                                    "content": "backup",
                                    "path": f"/mnt/user/{share.name}",
                                    "active": True,
                                    "enabled": True,
                                    "shared": True,
                                    "total": share.total_bytes,
                                    "used": share.used_bytes,
                                    "avail": share.free_bytes,
                                }
                            )
                if not api.supports_shares:
                    storages.append(
                        {
                            "storage": "shares-unavailable",
                            "type": "info",
                            "node": "unraid",
                            "content": "Shares unavailable",
                            "path": "",
                            "active": False,
                            "enabled": False,
                            "shared": True,
                            "total": 0,
                            "used": 0,
                            "avail": 0,
                        }
                    )

                return storages

            elif hypervisor_type == "proxmox":
                auth_method = (
                    ProxmoxAuthMethod.TOKEN if hypervisor["auth_method"] == "token" else ProxmoxAuthMethod.PASSWORD
                )

                api = ProxmoxAPI(
                    host=hypervisor["host"],
                    port=hypervisor.get("port", 8006),
                    token_id=hypervisor.get("token_id"),
                    token_secret=token_secret,
                    username=hypervisor.get("username"),
                    password=password,
                    auth_method=auth_method,
                    verify_ssl=hypervisor.get("verify_ssl", False),
                )

                # Authenticate if using password-based auth
                if auth_method == ProxmoxAuthMethod.PASSWORD:
                    api.authenticate()

                storages = api.list_storages(node)
                return [
                    {
                        "storage": s.storage,
                        "type": s.type,
                        "node": s.node,
                        "content": s.content,
                        "path": s.path,
                        "active": s.active,
                        "enabled": s.enabled,
                        "shared": s.shared,
                        "total": s.total,
                        "used": s.used,
                        "avail": s.avail,
                    }
                    for s in storages
                ]
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported hypervisor type: {hypervisor_type}")

        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"Failed to list storages for hypervisor {hypervisor_id}")
            raise HTTPException(status_code=500, detail=str(e))

    # ============ Hypervisor Jobs ============

    @app.get("/api/v1/hypervisor-jobs")
    def list_hypervisor_jobs(
        hypervisor_id: str | None = None,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List hypervisor backup jobs."""
        jobs = storage.list_hypervisor_jobs(hypervisor_id)

        # Enhance with hypervisor info and latest run
        for job in jobs:
            hypervisor = storage.get_hypervisor(job["hypervisor_id"])
            if hypervisor:
                job["hypervisor_name"] = hypervisor["name"]
                job["hypervisor_type"] = hypervisor["hypervisor_type"]

            latest = storage.get_latest_hypervisor_run(job["id"])
            job["last_status"] = latest["status"] if latest else None
            job["last_run"] = latest["started_at"] if latest else None

            # Add guest count for UI
            job["guest_count"] = len(job.get("guest_ids") or [])

        return jobs

    @app.post("/api/v1/hypervisor-jobs")
    async def create_hypervisor_job(
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Create a new hypervisor backup job."""
        body = await request.json()

        name = body.get("name", "").strip()
        hypervisor_id = body.get("hypervisor_id")
        # Accept both vmids and guest_ids
        guest_ids = body.get("vmids") or body.get("guest_ids") or []
        # Repository ID for Backer storage
        repository_id = body.get("repository_id")
        # Accept both mode/backup_mode and compress/compression
        backup_mode = body.get("mode") or body.get("backup_mode", "snapshot")
        compression = body.get("compress") or body.get("compression", "zstd")
        schedule_cron = body.get("schedule_cron")
        retention = body.get("retention", {})
        enabled = body.get("enabled", True)

        # Storage options
        copies_to_keep = body.get("copies_to_keep", 0)

        # Validate
        validate_name(name, "name")
        if not hypervisor_id:
            raise HTTPException(status_code=400, detail="hypervisor_id is required")
        # guest_ids can be empty to backup all guests
        if not repository_id:
            raise HTTPException(status_code=400, detail="repository_id is required")

        # Check hypervisor exists
        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        # Check repository exists and is SMB/NFS
        repository = storage.get_repository(repository_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        repo_type = repository.get("repo_type", "").lower()
        if repo_type not in ("smb", "nfs"):
            raise HTTPException(
                status_code=400,
                detail=f"Repository type '{repo_type}' is not supported for hypervisor backups. "
                "Only SMB or NFS repositories can be used.",
            )

        # Check for duplicate name
        if storage.get_hypervisor_job_by_name(name):
            raise HTTPException(status_code=400, detail="Job with this name already exists")

        # Note: Auto-import has been removed in favor of explicit adoption workflow
        # Users can now discover and adopt orphaned backups via the UI:
        # Storage -> Repository -> Orphaned Backups section

        job_id = str(uuid4())

        storage.add_hypervisor_job(
            job_id=job_id,
            name=name,
            hypervisor_id=hypervisor_id,
            guest_ids=guest_ids,
            repository_id=repository_id,
            backup_mode=backup_mode,
            compression=compression,
            schedule_cron=schedule_cron,
            retention=retention,
            enabled=enabled,
            copies_to_keep=copies_to_keep,
        )

        return {"id": job_id, "name": name, "status": "created"}

    @app.get("/api/v1/hypervisor-jobs/{job_id}")
    def get_hypervisor_job(
        job_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Get hypervisor job details."""
        job = storage.get_hypervisor_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    @app.put("/api/v1/hypervisor-jobs/{job_id}")
    async def update_hypervisor_job(
        job_id: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Update a hypervisor job."""
        job = storage.get_hypervisor_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        body = await request.json()

        update_kwargs: dict[str, Any] = {}
        if "name" in body:
            validate_name(body["name"], "name")
            update_kwargs["name"] = body["name"]
        # Accept both vmids and guest_ids
        if "vmids" in body or "guest_ids" in body:
            update_kwargs["guest_ids"] = body.get("vmids") or body.get("guest_ids")
        # Repository ID for Backer storage
        if "repository_id" in body:
            new_repo_id = body["repository_id"]
            repository = storage.get_repository(new_repo_id)
            if not repository:
                raise HTTPException(status_code=404, detail="Repository not found")
            repo_type = repository.get("repo_type", "").lower()
            if repo_type not in ("smb", "nfs"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Repository type '{repo_type}' is not supported for hypervisor backups. "
                    "Only SMB or NFS repositories can be used.",
                )
            update_kwargs["repository_id"] = new_repo_id
        # Accept both mode and backup_mode
        if "mode" in body or "backup_mode" in body:
            update_kwargs["backup_mode"] = body.get("mode") or body.get("backup_mode")
        # Accept both compress and compression
        if "compress" in body or "compression" in body:
            update_kwargs["compression"] = body.get("compress") or body.get("compression")
        if "schedule_cron" in body:
            update_kwargs["schedule_cron"] = body["schedule_cron"]
        if "retention" in body:
            update_kwargs["retention"] = body["retention"]
        if "enabled" in body:
            update_kwargs["enabled"] = body["enabled"]
        # Storage options
        if "copies_to_keep" in body:
            update_kwargs["copies_to_keep"] = body["copies_to_keep"]

        storage.update_hypervisor_job(job_id, **update_kwargs)

        return {"id": job_id, "status": "updated"}

    @app.delete("/api/v1/hypervisor-jobs/{job_id}")
    def delete_hypervisor_job(
        job_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Delete a hypervisor job and clean up all repository data.

        This permanently deletes the job configuration, all backup files,
        and all metadata from the repository. This prevents conflicts when
        creating new jobs with the same names or VMIDs.

        The cleanup runs in the background to avoid blocking the API.
        """
        job = storage.get_hypervisor_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        # Get repository and hypervisor info before deleting
        repository_id = job.get("repository_id")
        hypervisor_id = job.get("hypervisor_id")
        job_name = job.get("name", "Unknown")

        # Get all data we need before starting background task
        repository = None
        hypervisor = None

        if repository_id and hypervisor_id:
            repository = storage.get_repository(repository_id)
            hypervisor = storage.get_hypervisor(hypervisor_id)

        # Delete local database records immediately
        storage.delete_hypervisor_job(job_id)

        # Run cleanup in background if we have repository data
        if repository and hypervisor:
            # Make copies of the data to avoid threading issues with SQLite
            job_copy = dict(job)
            repository_copy = dict(repository)
            hypervisor_copy = dict(hypervisor)

            def cleanup_job_data(task: Task) -> dict[str, Any]:
                """Background task to clean up job data from repository.

                NOTE: We get a fresh Storage instance here to avoid SQLite threading issues.
                """
                import gc
                from pathlib import Path

                from backer.server.storage import Storage

                cleanup_errors = []
                task.message = f"Cleaning up backup data for job '{job_name}'"
                task.progress = 10

                # Get a fresh storage instance for this thread
                # Use the same database path as the main storage
                db_path = Path.home() / ".backer" / "backer.db"
                thread_storage = Storage(db_path)

                # Clean up backup files
                try:
                    task.message = f"Deleting backup files for '{job_name}'"
                    task.progress = 30
                    _cleanup_hypervisor_job_data(
                        repository=repository_copy,
                        hypervisor=hypervisor_copy,
                        job=job_copy,
                        storage=thread_storage,
                        repository_id=repository_id,
                    )
                    logger.info(f"Cleaned up backup files for job {job_id}")
                    task.progress = 60
                except Exception as e:
                    logger.warning(f"Failed to clean up backup files: {e}")
                    cleanup_errors.append(f"Backup files: {e}")

                # Clean up metadata files
                try:
                    task.message = f"Cleaning up metadata for '{job_name}'"
                    task.progress = 80
                    _cleanup_hypervisor_job_metadata(
                        repository=repository_copy,
                        hypervisor=hypervisor_copy,
                        job=job_copy,
                        storage=thread_storage,
                        repository_id=repository_id,
                    )
                    logger.info(f"Cleaned up metadata files for job {job_id}")
                    task.progress = 100
                except Exception as e:
                    logger.warning(f"Failed to clean up metadata files: {e}")
                    cleanup_errors.append(f"Metadata: {e}")

                # Explicitly close the thread's storage connection to release SQLite lock
                del thread_storage
                gc.collect()  # Force garbage collection to close database connection immediately

                result: dict[str, Any] = {
                    "id": job_id,
                    "status": "cleanup_complete",
                    "success": len(cleanup_errors) == 0,
                }
                if cleanup_errors:
                    result["cleanup_warnings"] = cleanup_errors

                task.message = f"Cleanup complete for '{job_name}'"
                return result

            task_manager = get_task_manager()
            task = task_manager.submit(
                task_type="delete_hypervisor_job",
                description=f"Deleting backup data for job '{job_name}'",
                func=cleanup_job_data,
            )

            return {
                "id": job_id,
                "status": "deleted",
                "cleanup_task_id": task.id,
                "message": "Job deleted. Cleanup running in background.",
            }

        # No repository data to clean up
        return {"id": job_id, "status": "deleted"}

    def _auto_import_hypervisor_jobs(
        storage: Storage,
        repository: dict[str, Any],
        repository_id: str,
        hypervisor: dict[str, Any],
        hypervisor_id: str,
    ) -> int:
        """Auto-import existing job configurations from repository metadata.

        When a user creates a new job for a hypervisor+repository combination,
        this function checks if there are existing job configurations stored
        in the repository metadata (.backer/hypervisor_jobs/) and imports them
        automatically. This enables disaster recovery - if the backer server
        is reinstalled, jobs are auto-recovered when recreating them.

        Args:
            storage: Database storage
            repository: Repository dict
            repository_id: Repository ID
            hypervisor: Hypervisor dict
            hypervisor_id: Hypervisor ID

        Returns:
            Number of jobs imported
        """
        import json as json_module
        import subprocess
        import tempfile

        from backer.server.repositories import smb_list_files, smb_read_file

        # Check if we already have jobs for this hypervisor+repo combination
        existing_jobs = storage.list_hypervisor_jobs()
        has_existing = any(
            j.get("hypervisor_id") == hypervisor_id and j.get("repository_id") == repository_id for j in existing_jobs
        )
        if has_existing:
            # Already have jobs, skip auto-import
            return 0

        repo_type = repository.get("repo_type", "").lower()
        server = repository.get("server", "")
        hv_name = hypervisor.get("name", "unknown")
        hv_host = hypervisor.get("host", "")

        # Sanitize hypervisor name for path matching
        safe_hv_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in hv_name)

        imported_count = 0

        try:
            if repo_type == "smb":
                share = repository.get("share", "")
                subpath = repository.get("path", "")
                username = repository.get("username")
                password = storage.get_storage_password(repository_id)
                domain = repository.get("domain")

                # Path to .backer/ in the hypervisor folder
                backer_path = (
                    f"{subpath}/Hypervisors/{safe_hv_name}/.backer"
                    if subpath
                    else f"Hypervisors/{safe_hv_name}/.backer"
                )
                jobs_path = f"{backer_path}/hypervisor_jobs"

                # Get list of deleted jobs to skip
                deleted_job_ids = _get_deleted_job_ids_smb(server, share, backer_path, username, password, domain)
                if deleted_job_ids:
                    logger.debug(f"Found {len(deleted_job_ids)} deleted jobs to skip")

                ok, job_files = smb_list_files(server, share, jobs_path, username, password, domain)
                if not ok:
                    logger.debug(f"No job metadata found at {jobs_path}")
                    return 0

                for job_file in job_files:
                    if not job_file.endswith(".json"):
                        continue

                    ok2, job_content = smb_read_file(
                        server, share, f"{jobs_path}/{job_file}", username, password, domain
                    )
                    if not ok2:
                        continue

                    try:
                        job_data = json_module.loads(job_content)
                        imported_count += _import_single_job(
                            storage, job_data, hypervisor_id, repository_id, hv_name, hv_host, deleted_job_ids
                        )
                    except (json_module.JSONDecodeError, Exception) as e:
                        logger.warning(f"Failed to import job from {job_file}: {e}")

            elif repo_type == "nfs":
                export = repository.get("share", "")
                mount_point = tempfile.mkdtemp(prefix="backer_auto_import_")
                mounted = False

                try:
                    mount_cmd = [
                        "sudo",
                        "-n",
                        "mount",
                        "-t",
                        "nfs",
                        "-o",
                        "soft,timeo=50,retrans=2,ro",
                        f"{server}:{export}",
                        mount_point,
                    ]
                    result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=60)
                    if result.returncode != 0:
                        logger.debug(f"Could not mount NFS for auto-import: {result.stderr.strip()}")
                        return 0

                    mounted = True

                    # Path to .backer/ in the hypervisor folder
                    backer_path = Path(mount_point) / "Hypervisors" / safe_hv_name / ".backer"
                    jobs_dir = backer_path / "hypervisor_jobs"

                    if not jobs_dir.exists():
                        logger.debug(f"No job metadata found at {jobs_dir}")
                        return 0

                    # Get list of deleted jobs to skip
                    deleted_job_ids = _get_deleted_job_ids_from_path(backer_path)
                    if deleted_job_ids:
                        logger.debug(f"Found {len(deleted_job_ids)} deleted jobs to skip")

                    for job_file in jobs_dir.glob("*.json"):
                        try:
                            job_data = json_module.loads(job_file.read_text())
                            imported_count += _import_single_job(
                                storage, job_data, hypervisor_id, repository_id, hv_name, hv_host, deleted_job_ids
                            )
                        except (json_module.JSONDecodeError, Exception) as e:
                            logger.warning(f"Failed to import job from {job_file.name}: {e}")

                finally:
                    if mounted:
                        try:
                            subprocess.run(["sudo", "-n", "umount", "-l", mount_point], capture_output=True, timeout=30)
                        except Exception:
                            pass
                    try:
                        Path(mount_point).rmdir()
                    except Exception:
                        pass

        except Exception as e:
            logger.warning(f"Auto-import failed: {e}")

        return imported_count

    def _import_single_job(
        storage: Storage,
        job_data: dict[str, Any],
        hypervisor_id: str,
        repository_id: str,
        hv_name: str,
        hv_host: str,
        deleted_job_ids: set[str] | None = None,
    ) -> int:
        """Import a single job from metadata.

        Args:
            storage: Database storage
            job_data: Job metadata dict
            hypervisor_id: Target hypervisor ID
            repository_id: Target repository ID
            hv_name: Hypervisor name for matching
            hv_host: Hypervisor host for matching
            deleted_job_ids: Set of job IDs that have been deleted (skip these)

        Returns:
            1 if imported, 0 if skipped.
        """
        job_id = job_data.get("job_id")
        job_name = job_data.get("name")

        if not job_id or not job_name:
            return 0

        # Check if job was previously deleted - don't re-import
        if deleted_job_ids and job_id in deleted_job_ids:
            logger.debug(f"Skipping job '{job_name}' - previously deleted")
            return 0

        # Check if job already exists by ID
        if storage.get_hypervisor_job(job_id):
            return 0

        # Check if job exists by name
        if storage.get_hypervisor_job_by_name(job_name):
            return 0

        # Verify this job was for the same hypervisor (by ID, name, or host)
        job_hv_id = job_data.get("hypervisor_id")
        job_hv_name = job_data.get("hypervisor_name")
        job_hv_host = job_data.get("hypervisor_host")

        # Must match by at least one identifier
        if not (job_hv_id == hypervisor_id or job_hv_name == hv_name or job_hv_host == hv_host):
            logger.debug(f"Skipping job '{job_name}' - hypervisor mismatch")
            return 0

        # Import the job
        guest_ids = job_data.get("guest_ids") or []

        storage.add_hypervisor_job(
            job_id=job_id,
            name=job_name,
            hypervisor_id=hypervisor_id,
            guest_ids=guest_ids,
            repository_id=repository_id,
            backup_mode=job_data.get("backup_mode", "snapshot"),
            compression=job_data.get("compression", "zstd"),
            schedule_cron=job_data.get("schedule_cron"),
            enabled=job_data.get("enabled", True),
            copies_to_keep=job_data.get("copies_to_keep", 0),
        )

        logger.info(f"Auto-imported job '{job_name}' from repository metadata")
        return 1

    def _cleanup_hypervisor_job_data(
        repository: dict[str, Any],
        hypervisor: dict[str, Any],
        job: dict[str, Any],
        storage: Storage,
        repository_id: str,
    ) -> None:
        """Clean up backup files for a specific job from the repository.

        IMPORTANT: Multiple jobs from the same hypervisor share the same dump/ folder.
        We only delete backup files that belong to THIS job based on:
        - The guest IDs configured in the job
        - The timestamps from the job's run history

        We do NOT delete the shared .backer/ metadata folder since other jobs need it.
        """
        from datetime import datetime

        repo_type = repository.get("repo_type", "").lower()
        server = repository.get("server", "")
        hypervisor_type = hypervisor.get("hypervisor_type", "proxmox")

        # Get hypervisor name for path construction
        hv_name = hypervisor.get("name", "unknown")
        job_id = job.get("id")

        # Get all runs for this job to identify which backup files belong to it
        runs = storage.get_hypervisor_runs(job_id=job_id, limit=10000)

        # Build list of filenames to delete - prefer stored filenames, fall back to timestamp matching
        filenames_to_delete: list[str] = []
        timestamp_fallback: list[tuple[int, datetime]] = []

        for run in runs:
            backup_filename = run.get("backup_filename")
            if backup_filename:
                # New approach: use the exact filename stored during backup
                filenames_to_delete.append(backup_filename)
            else:
                # Legacy fallback for runs without stored filename
                guest_id = run.get("guest_id")
                started_at = run.get("started_at")
                if guest_id and started_at:
                    try:
                        dt = datetime.fromisoformat(started_at)
                        timestamp_fallback.append((guest_id, dt))
                    except ValueError:
                        pass

        if not filenames_to_delete and not timestamp_fallback:
            logger.info(f"No backup runs found for job {job.get('name')} - nothing to clean up")
            return

        # Log what we're about to clean up
        for filename in filenames_to_delete:
            logger.info(f"  - Will delete backup file/folder: {filename}")
        for guest_id, dt in timestamp_fallback:
            logger.info(f"  - Will look for VMID {guest_id} backup from {dt.isoformat()} (legacy timestamp match)")

        # Hyper-V uses a different folder structure than Proxmox
        # Structure: {repo_path}/Hypervisors/{hv_name}/{vm_name}/{timestamp}/{vm_name}/
        # The backup_filename stores the full UNC path to the VM export folder
        if hypervisor_type in ("hyperv", "hyperv-cluster"):
            if repo_type == "smb":
                share = repository.get("share", "")
                username = repository.get("username")
                domain = repository.get("domain")
                repo_password = storage.get_storage_password(repository_id)

                if not server or not share:
                    logger.warning("Cannot clean up SMB: missing server or share")
                    return

                logger.info(f"[HYPERV CLEANUP] Cleaning up Hyper-V backup folders for job {job.get('name')}")
                _cleanup_hyperv_smb_job_files(server, share, username, repo_password, domain, filenames_to_delete)
            else:
                logger.warning(f"Hyper-V backup cleanup not supported for repo type: {repo_type}")
            return

        # Proxmox/standard cleanup below
        if repo_type == "nfs":
            # NFS storage: Proxmox mounts the export root and vzdump creates dump/ there
            # So backups are at: {export}/dump/vzdump-*.vma.zst
            export = repository.get("share", "")  # For NFS, 'share' contains the export path
            if not server or not export:
                logger.warning("Cannot clean up NFS: missing server or export")
                return

            dump_path = "dump"  # Proxmox creates dump/ at the NFS export root
            logger.info(f"NFS cleanup: {server}:{export}/{dump_path}")
            _cleanup_nfs_job_files(server, export, dump_path, filenames_to_delete, timestamp_fallback)

        elif repo_type == "smb":
            # SMB storage: Proxmox mounts at {share}/{subdir}/Hypervisors/{hv_name}
            # and creates dump/ inside that
            share = repository.get("share", "")
            subdir = repository.get("path", "").strip("/")
            username = repository.get("username")
            domain = repository.get("domain")
            repo_password = storage.get_storage_password(repository_id)

            if not server or not share:
                logger.warning("Cannot clean up SMB: missing server or share")
                return

            # Build full path: {subdir}/Hypervisors/{hv_name}/dump
            hv_dump_path = f"Hypervisors/{hv_name}/dump"
            if subdir:
                full_dump_path = f"{subdir}/{hv_dump_path}"
            else:
                full_dump_path = hv_dump_path

            logger.info(f"SMB cleanup: //{server}/{share}/{full_dump_path}")
            _cleanup_smb_job_files(
                server, share, username, repo_password, domain, full_dump_path, filenames_to_delete, timestamp_fallback
            )
        else:
            logger.warning(f"Repository cleanup not supported for type: {repo_type}")

    def _cleanup_hypervisor_job_metadata(
        repository: dict[str, Any],
        hypervisor: dict[str, Any],
        job: dict[str, Any],
        storage: Storage,
        repository_id: str,
    ) -> None:
        """Clean up job metadata files from the repository.

        This removes:
        - .backer/hypervisor_jobs/{job_id}.json
        - .backer/hypervisor_backups/{vmid}/runs/{run_id}.json for runs belonging to this job

        Guest metadata (.backer/hypervisor_backups/{vmid}/guest.json) is preserved
        as other jobs may reference the same guests.
        """
        repo_type = repository.get("repo_type", "").lower()
        server = repository.get("server", "")
        hv_name = hypervisor.get("name", "unknown")
        job_id = job.get("id", "")

        # Get run IDs for this job from the database before they're deleted
        runs = storage.get_hypervisor_runs(job_id=job_id, limit=10000)
        run_ids = [(r.get("guest_id"), r.get("id")) for r in runs if r.get("id")]

        logger.info(f"[METADATA CLEANUP] Cleaning up metadata for job '{job.get('name')}' ({len(run_ids)} run records)")

        if repo_type == "nfs":
            export = repository.get("share", "")
            if not server or not export:
                logger.warning("Cannot clean up NFS metadata: missing server or export")
                return
            _cleanup_nfs_job_metadata(server, export, hv_name, job_id, run_ids)

        elif repo_type == "smb":
            share = repository.get("share", "")
            subdir = repository.get("path", "").strip("/")
            username = repository.get("username")
            domain = repository.get("domain")
            repo_password = storage.get_storage_password(repository_id)

            if not server or not share:
                logger.warning("Cannot clean up SMB metadata: missing server or share")
                return

            _cleanup_smb_job_metadata(server, share, username, repo_password, domain, subdir, hv_name, job_id, run_ids)
        else:
            logger.warning(f"Metadata cleanup not supported for type: {repo_type}")

    def _record_deleted_job(backer_path: Path, job_id: str) -> None:
        """Record a job deletion in the deleted_jobs.json file.

        This ensures deleted jobs won't be re-imported even if the job
        metadata file deletion fails.

        Args:
            backer_path: Path to the .backer metadata folder
            job_id: Job ID being deleted
        """
        import json as json_module

        deleted_file = backer_path / "deleted_jobs.json"

        try:
            # Read existing deleted jobs
            if deleted_file.exists():
                deleted_jobs = json_module.loads(deleted_file.read_text(encoding="utf-8"))
            else:
                deleted_jobs = {"jobs": []}

            # Check if already recorded
            existing_ids = {j.get("job_id") for j in deleted_jobs.get("jobs", [])}
            if job_id in existing_ids:
                return

            # Add the deletion record
            deleted_jobs.setdefault("jobs", []).append(
                {
                    "job_id": job_id,
                    "deleted_at": tz.get_now().isoformat(),
                }
            )

            # Write back
            deleted_file.parent.mkdir(parents=True, exist_ok=True)
            deleted_file.write_text(json_module.dumps(deleted_jobs, indent=2), encoding="utf-8")
            logger.info(f"[METADATA] Recorded job {job_id} as deleted")

        except Exception as e:
            logger.warning(f"[METADATA] Failed to record job deletion: {e}")

    def _record_deleted_job_smb(
        server: str,
        share: str,
        auth_parts: list[str],
        backer_path: str,
        job_id: str,
    ) -> None:
        """Record a job deletion in the deleted_jobs.json file on SMB share.

        Args:
            server: SMB server
            share: SMB share name
            auth_parts: Authentication arguments for smbclient
            backer_path: Path to .backer folder within the share
            job_id: Job ID being deleted
        """
        import json as json_module
        import subprocess
        import tempfile

        deleted_file_path = f"{backer_path}/deleted_jobs.json"

        try:
            # Read existing file
            with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as tmp:
                tmp_path = tmp.name

            # Try to download existing file
            get_cmd = ["smbclient", f"//{server}/{share}", *auth_parts, "-c", f'get "{deleted_file_path}" {tmp_path}']
            result = subprocess.run(get_cmd, capture_output=True, timeout=60, text=True)

            if result.returncode == 0:
                with open(tmp_path, encoding="utf-8") as f:
                    deleted_jobs = json_module.load(f)
            else:
                deleted_jobs = {"jobs": []}

            # Check if already recorded
            existing_ids = {j.get("job_id") for j in deleted_jobs.get("jobs", [])}
            if job_id in existing_ids:
                return

            # Add the deletion record
            deleted_jobs.setdefault("jobs", []).append(
                {
                    "job_id": job_id,
                    "deleted_at": tz.get_now().isoformat(),
                }
            )

            # Write to temp file and upload
            with open(tmp_path, "w", encoding="utf-8") as f:
                json_module.dump(deleted_jobs, f, indent=2)

            put_cmd = ["smbclient", f"//{server}/{share}", *auth_parts, "-c", f'put {tmp_path} "{deleted_file_path}"']
            result = subprocess.run(put_cmd, capture_output=True, timeout=60, text=True)

            if result.returncode == 0:
                logger.info(f"[SMB METADATA] Recorded job {job_id} as deleted")
            else:
                logger.warning(f"[SMB METADATA] Failed to write deleted_jobs.json: {result.stderr}")

        except Exception as e:
            logger.warning(f"[SMB METADATA] Failed to record job deletion: {e}")
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

    def _get_deleted_job_ids_from_path(backer_path: Path) -> set[str]:
        """Read deleted job IDs from a .backer folder.

        Args:
            backer_path: Path to the .backer metadata folder

        Returns:
            Set of deleted job IDs
        """
        import json as json_module

        deleted_file = backer_path / "deleted_jobs.json"
        try:
            if deleted_file.exists():
                deleted_jobs = json_module.loads(deleted_file.read_text(encoding="utf-8"))
                return {j.get("job_id") for j in deleted_jobs.get("jobs", []) if j.get("job_id")}
        except Exception as e:
            logger.warning(f"[METADATA] Failed to read deleted_jobs.json: {e}")
        return set()

    def _get_deleted_job_ids_smb(
        server: str,
        share: str,
        backer_path: str,
        username: str | None,
        password: str | None,
        domain: str | None,
    ) -> set[str]:
        """Read deleted job IDs from a .backer folder on SMB share.

        Args:
            server: SMB server
            share: SMB share name
            backer_path: Path to .backer folder within the share
            username: SMB username
            password: SMB password
            domain: SMB domain

        Returns:
            Set of deleted job IDs
        """
        import json as json_module

        from backer.server.repositories import smb_read_file

        deleted_file_path = f"{backer_path}/deleted_jobs.json"
        ok, content = smb_read_file(server, share, deleted_file_path, username, password, domain)

        if ok and content:
            try:
                deleted_jobs = json_module.loads(content)
                return {j.get("job_id") for j in deleted_jobs.get("jobs", []) if j.get("job_id")}
            except json_module.JSONDecodeError:
                pass

        return set()

    def _cleanup_nfs_job_metadata(
        server: str,
        export: str,
        hypervisor_name: str,
        job_id: str,
        run_ids: list[tuple[int | None, str]],
    ) -> None:
        """Clean up job metadata files from an NFS share."""
        import os
        import subprocess
        import tempfile

        # Sanitize hypervisor name
        safe_hv_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in hypervisor_name)

        logger.info(f"[NFS METADATA] Cleaning up job metadata from {server}:{export}")

        mount_point = None
        try:
            mount_point = tempfile.mkdtemp(prefix="backer_meta_cleanup_")
            nfs_source = f"{server}:{export}"

            mount_cmd = ["sudo", "-n", "mount", "-t", "nfs", "-o", "rw", nfs_source, mount_point]
            result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                logger.error(f"[NFS METADATA] Mount failed: {result.stderr.strip()}")
                return

            # Path to .backer metadata folder
            backer_path = Path(mount_point) / "Hypervisors" / safe_hv_name / ".backer"

            # Record the job as deleted FIRST to prevent re-import
            # This is done before deleting the job file so even if deletion fails,
            # the job won't be re-imported on a fresh install
            _record_deleted_job(backer_path, job_id)

            # Delete job metadata file
            job_file = backer_path / "hypervisor_jobs" / f"{job_id}.json"
            if job_file.exists():
                job_file.unlink()
                logger.info(f"[NFS METADATA] Deleted job metadata: {job_file.name}")

            # Delete run record files
            deleted_runs = 0
            for guest_id, run_id in run_ids:
                if guest_id:
                    run_file = backer_path / "hypervisor_backups" / str(guest_id) / "runs" / f"{run_id}.json"
                    if run_file.exists():
                        run_file.unlink()
                        deleted_runs += 1

            if deleted_runs > 0:
                logger.info(f"[NFS METADATA] Deleted {deleted_runs} run record files")

            logger.info("[NFS METADATA] Job metadata cleanup completed")

        except subprocess.TimeoutExpired:
            logger.error("[NFS METADATA] NFS mount timed out")
        except Exception as e:
            logger.error(f"[NFS METADATA] Error: {e}", exc_info=True)
        finally:
            if mount_point:
                try:
                    subprocess.run(["sudo", "-n", "umount", mount_point], capture_output=True, timeout=30)
                    os.rmdir(mount_point)
                except Exception:
                    try:
                        subprocess.run(["sudo", "-n", "umount", "-l", mount_point], capture_output=True, timeout=10)
                        os.rmdir(mount_point)
                    except Exception as e:
                        logger.warning(f"[NFS METADATA] Could not unmount: {e}")

    def _cleanup_smb_job_metadata(
        server: str,
        share: str,
        username: str | None,
        password: str | None,
        domain: str | None,
        subdir: str,
        hypervisor_name: str,
        job_id: str,
        run_ids: list[tuple[int | None, str]],
    ) -> None:
        """Clean up job metadata files from an SMB share."""
        import subprocess

        # Sanitize hypervisor name
        safe_hv_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in hypervisor_name)

        # Build smbclient auth
        if username:
            if domain:
                auth_str = f"{domain}\\{username}%{password or ''}"
            else:
                auth_str = f"{username}%{password or ''}"
            auth_parts = ["-U", auth_str]
        else:
            auth_parts = ["-N"]

        def run_smb_cmd(cmd: str) -> tuple[bool, str]:
            full_cmd = ["smbclient", f"//{server}/{share}", *auth_parts, "-c", cmd]
            try:
                result = subprocess.run(full_cmd, capture_output=True, timeout=60, text=True)
                return result.returncode == 0, result.stderr or result.stdout
            except Exception as e:
                return False, str(e)

        # Build path to .backer folder
        if subdir:
            backer_path = f"{subdir}/Hypervisors/{safe_hv_name}/.backer"
        else:
            backer_path = f"Hypervisors/{safe_hv_name}/.backer"

        logger.info(f"[SMB METADATA] Cleaning up job metadata from //{server}/{share}/{backer_path}")

        # Record the job as deleted FIRST to prevent re-import
        # This is done before deleting the job file so even if deletion fails,
        # the job won't be re-imported on a fresh install
        _record_deleted_job_smb(server, share, auth_parts, backer_path, job_id)

        # Delete job metadata file
        job_file_path = f"{backer_path}/hypervisor_jobs/{job_id}.json"
        ok, _ = run_smb_cmd(f'del "{job_file_path}"')
        if ok:
            logger.info("[SMB METADATA] Deleted job metadata file")

        # Delete run record files
        deleted_runs = 0
        for guest_id, run_id in run_ids:
            if guest_id:
                run_file_path = f"{backer_path}/hypervisor_backups/{guest_id}/runs/{run_id}.json"
                ok, _ = run_smb_cmd(f'del "{run_file_path}"')
                if ok:
                    deleted_runs += 1

        if deleted_runs > 0:
            logger.info(f"[SMB METADATA] Deleted {deleted_runs} run record files")

        logger.info("[SMB METADATA] Job metadata cleanup completed")

    def _cleanup_nfs_job_files(
        server: str,
        export: str,
        dump_path: str,
        filenames_to_delete: list[str],
        timestamp_fallback: list[tuple[int, "datetime"]],
    ) -> None:
        """Clean up specific backup files from an NFS share.

        Deletes files by exact filename when available (new method),
        falling back to timestamp matching for legacy runs.
        """
        import os
        import re
        import subprocess
        import tempfile
        from datetime import datetime

        total_files = len(filenames_to_delete) + len(timestamp_fallback)
        logger.info(f"[NFS CLEANUP] Starting cleanup for {total_files} backup(s)")
        logger.info(f"[NFS CLEANUP] Target: {server}:{export}/{dump_path}")
        logger.info(
            f"[NFS CLEANUP] Direct filenames: {len(filenames_to_delete)}, Timestamp fallback: {len(timestamp_fallback)}"
        )

        mount_point = None
        try:
            # Create temporary mount point
            mount_point = tempfile.mkdtemp(prefix="backer_cleanup_")
            nfs_source = f"{server}:{export}"
            logger.info(f"[NFS CLEANUP] Mounting {nfs_source} to {mount_point}")

            # Mount NFS with write access
            mount_cmd = ["sudo", "-n", "mount", "-t", "nfs", "-o", "rw", nfs_source, mount_point]
            result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                logger.error(f"[NFS CLEANUP] Mount failed: {result.stderr.strip()}")
                return

            logger.info("[NFS CLEANUP] Mount successful")

            full_dump_path = Path(mount_point) / dump_path
            logger.info(f"[NFS CLEANUP] Looking for files in: {full_dump_path}")

            if not full_dump_path.exists():
                logger.warning(f"[NFS CLEANUP] Dump path does not exist: {full_dump_path}")
                try:
                    contents = list(Path(mount_point).iterdir())
                    logger.info(f"[NFS CLEANUP] Mount point contents: {[e.name for e in contents[:20]]}")
                except Exception as e:
                    logger.warning(f"[NFS CLEANUP] Could not list mount point: {e}")
                return

            deleted_count = 0

            # Helper function to delete a backup file and its associated files
            def delete_backup_file(file_path: Path) -> bool:
                nonlocal deleted_count
                try:
                    if file_path.exists():
                        file_path.unlink()
                        deleted_count += 1
                        logger.info(f"[NFS CLEANUP] DELETED: {file_path.name}")

                        # Also delete .notes file if exists
                        notes_file = Path(str(file_path) + ".notes")
                        if notes_file.exists():
                            notes_file.unlink()
                            logger.info(f"[NFS CLEANUP] DELETED notes: {notes_file.name}")

                        # Delete .log file (same base name)
                        base_name = (
                            file_path.name.replace(".vma.zst", "")
                            .replace(".vma.gz", "")
                            .replace(".vma.lzo", "")
                            .replace(".vma", "")
                            .replace(".tar.zst", "")
                            .replace(".tar.gz", "")
                            .replace(".tar.lzo", "")
                            .replace(".tar", "")
                        )
                        log_file = file_path.parent / f"{base_name}.log"
                        if log_file.exists():
                            log_file.unlink()
                            logger.info(f"[NFS CLEANUP] DELETED log: {log_file.name}")
                        return True
                    else:
                        logger.warning(f"[NFS CLEANUP] File not found: {file_path.name}")
                        return False
                except Exception as e:
                    logger.error(f"[NFS CLEANUP] Failed to delete {file_path.name}: {type(e).__name__}: {e}")
                    return False

            # Method 1: Delete by exact filename (preferred)
            for filename in filenames_to_delete:
                logger.info(f"[NFS CLEANUP] Deleting by filename: {filename}")
                file_path = full_dump_path / filename
                delete_backup_file(file_path)

            # Method 2: Fallback to timestamp matching for legacy runs
            if timestamp_fallback:
                logger.info(f"[NFS CLEANUP] Processing {len(timestamp_fallback)} legacy timestamp matches")
                for guest_id, timestamp in timestamp_fallback:
                    logger.info(f"[NFS CLEANUP] Looking for VMID {guest_id} from {timestamp}")

                    for entry in full_dump_path.iterdir():
                        if not entry.is_file():
                            continue

                        match = re.match(
                            rf"vzdump-(qemu|lxc)-{guest_id}-(\d{{4}}_\d{{2}}_\d{{2}})-"
                            rf"(\d{{2}}_\d{{2}}_\d{{2}})\.(vma|tar)(?:\.(zst|gz|lzo))?$",
                            entry.name,
                        )
                        if match:
                            file_date = match.group(2)
                            file_time = match.group(3)
                            try:
                                file_dt = datetime.strptime(f"{file_date}_{file_time}", "%Y_%m_%d_%H_%M_%S")
                                timestamp_naive = timestamp.replace(tzinfo=None)
                                time_diff = abs((file_dt - timestamp_naive).total_seconds())

                                # Match if within 10 minutes
                                if time_diff < 600:
                                    logger.info(f"[NFS CLEANUP] Timestamp match (diff: {time_diff:.0f}s): {entry.name}")
                                    delete_backup_file(entry)
                            except ValueError:
                                continue

            if deleted_count > 0:
                logger.info(f"[NFS CLEANUP] SUCCESS: Deleted {deleted_count} backup files")
            else:
                logger.warning("[NFS CLEANUP] No matching backup files found to delete")

        except subprocess.TimeoutExpired:
            logger.error("[NFS CLEANUP] NFS mount timed out")
        except Exception as e:
            logger.error(f"[NFS CLEANUP] Error: {e}", exc_info=True)
        finally:
            if mount_point:
                logger.info(f"[NFS CLEANUP] Unmounting {mount_point}")
                try:
                    subprocess.run(["sudo", "-n", "umount", mount_point], capture_output=True, timeout=30)
                    os.rmdir(mount_point)
                    logger.info("[NFS CLEANUP] Unmount successful")
                except Exception:
                    try:
                        subprocess.run(["sudo", "-n", "umount", "-l", mount_point], capture_output=True, timeout=10)
                        os.rmdir(mount_point)
                        logger.info("[NFS CLEANUP] Lazy unmount successful")
                    except Exception as e:
                        logger.warning(f"[NFS CLEANUP] Could not unmount: {e}")

    def _cleanup_hyperv_smb_job_files(
        server: str,
        share: str,
        username: str | None,
        password: str | None,
        domain: str | None,
        backup_paths: list[str],
    ) -> None:
        """Clean up Hyper-V backup folders from an SMB share.

        For Hyper-V, backup_paths contains full UNC paths to VM export folders like:
        \\\\server\\share\\Backer\\Hypervisors\\{hv_name}\\testwin11\\20251210_191813\\testwin11

        We need to delete the timestamp folder (parent of the VM name folder), e.g.:
        \\\\server\\share\\Backer\\Hypervisors\\{hv_name}\\testwin11\\20251210_191813\\

        Structure: {repo_path}/Hypervisors/{hv_name}/{vm_name}/{timestamp}/{vm_name}/
        """
        import subprocess

        from backer.server.repositories import SMBBrowser

        # Build smbclient auth
        if username:
            if domain:
                auth_str = f"{domain}\\{username}%{password or ''}"
            else:
                auth_str = f"{username}%{password or ''}"
            auth_parts = ["-U", auth_str]
        else:
            auth_parts = ["-N"]

        def run_smb_cmd(cmd: str, timeout: int = 60) -> tuple[bool, str]:
            """Run an smbclient command and return success status and output."""
            full_cmd = ["smbclient", f"//{server}/{share}", *auth_parts, "-c", cmd]
            try:
                result = subprocess.run(full_cmd, capture_output=True, timeout=timeout, text=True)
                return result.returncode == 0, result.stderr or result.stdout
            except subprocess.TimeoutExpired:
                return False, "Command timed out"
            except Exception as e:
                return False, str(e)

        def delete_folder_recursive(smb_path: str, depth: int = 0, max_depth: int = 50) -> bool:
            """Recursively delete a folder and all its contents.

            Args:
                smb_path: Path to delete
                depth: Current recursion depth
                max_depth: Maximum recursion depth to prevent stack overflow
            """
            if depth > max_depth:
                logger.warning(f"[HYPERV CLEANUP] Max recursion depth ({max_depth}) reached at {smb_path}")
                return False

            # List directory contents using SMBBrowser for proper file type detection
            success, entries = SMBBrowser.list_directory(server, share, smb_path, username, password, domain)
            if not success:
                logger.debug(f"[HYPERV CLEANUP] Could not list {smb_path}: {entries}")
                return False

            # Delete contents first
            for entry in entries:
                if entry.name in [".", ".."]:
                    continue
                entry_path = f"{smb_path}/{entry.name}"
                if entry.is_dir:
                    delete_folder_recursive(entry_path, depth + 1, max_depth)
                    run_smb_cmd(f'rmdir "{entry_path}"')
                else:
                    run_smb_cmd(f'del "{entry_path}"')

            return True

        logger.info(f"[HYPERV CLEANUP] Starting cleanup for {len(backup_paths)} Hyper-V backup(s)")
        deleted_count = 0

        for backup_path in backup_paths:
            if not backup_path:
                continue

            # backup_path is the full UNC path to the VM export folder:
            # \\server\share\Backer\Hypervisors\hyperv\testwin11\20251210_191813\testwin11
            # We need to delete the timestamp folder (parent)
            # Convert UNC path to SMB relative path
            unc_prefix = f"\\\\{server}\\{share}"
            if not backup_path.startswith(unc_prefix):
                # Try with forward slashes
                unc_prefix_fwd = f"//{server}/{share}"
                if backup_path.startswith(unc_prefix_fwd):
                    smb_path = backup_path[len(unc_prefix_fwd) :].lstrip("/\\")
                else:
                    logger.warning(f"[HYPERV CLEANUP] Path doesn't match share: {backup_path}")
                    continue
            else:
                smb_path = backup_path[len(unc_prefix) :].lstrip("/\\")

            # Normalize path separators
            smb_path = smb_path.replace("\\", "/")

            # The smb_path is like: Backer/Hypervisors/{hv_name}/testwin11/20251210_191813/testwin11
            # We want to delete the timestamp folder: Backer/Hypervisors/{hv_name}/testwin11/20251210_191813
            parts = smb_path.rstrip("/").split("/")
            if len(parts) >= 2:
                # Remove the last part (VM name folder created by Export-VM) to get timestamp folder
                timestamp_folder = "/".join(parts[:-1])
            else:
                timestamp_folder = smb_path

            logger.info(f"[HYPERV CLEANUP] Deleting: {timestamp_folder}")

            # Recursively delete the timestamp folder and all its contents
            delete_folder_recursive(timestamp_folder)

            # Delete the timestamp folder itself
            ok, output = run_smb_cmd(f'rmdir "{timestamp_folder}"')
            if ok:
                deleted_count += 1
                logger.info(f"[HYPERV CLEANUP] DELETED folder: {timestamp_folder}")
            else:
                # Try to delete anyway - folder might already be empty
                if "NT_STATUS_NO_SUCH_FILE" in output or "NT_STATUS_OBJECT_NAME_NOT_FOUND" in output:
                    logger.debug(f"[HYPERV CLEANUP] Folder already deleted: {timestamp_folder}")
                else:
                    logger.warning(f"[HYPERV CLEANUP] Failed to delete folder {timestamp_folder}: {output}")

        logger.info(f"[HYPERV CLEANUP] Cleanup complete. Deleted {deleted_count} folder(s)")

    def _cleanup_smb_job_files(
        server: str,
        share: str,
        username: str | None,
        password: str | None,
        domain: str | None,
        dump_path: str,
        filenames_to_delete: list[str],
        timestamp_fallback: list[tuple[int, "datetime"]],
    ) -> None:
        """Clean up specific backup files from an SMB share.

        Deletes files by exact filename when available (new method),
        falling back to timestamp matching for legacy runs.
        """
        import re
        import subprocess
        from datetime import datetime

        # Build smbclient auth
        if username:
            if domain:
                auth_str = f"{domain}\\{username}%{password or ''}"
            else:
                auth_str = f"{username}%{password or ''}"
            auth_parts = ["-U", auth_str]
        else:
            auth_parts = ["-N"]

        def run_smb_cmd(cmd: str) -> tuple[bool, str]:
            """Run an smbclient command and return success status and output."""
            full_cmd = ["smbclient", f"//{server}/{share}", *auth_parts, "-c", cmd]
            try:
                result = subprocess.run(full_cmd, capture_output=True, timeout=60, text=True)
                return result.returncode == 0, result.stderr or result.stdout
            except subprocess.TimeoutExpired:
                return False, "Command timed out"
            except Exception as e:
                return False, str(e)

        def delete_backup_file(filename: str) -> bool:
            """Delete a backup file and its associated .notes and .log files."""
            nonlocal deleted_count
            del_ok, del_out = run_smb_cmd(f'del "{dump_path}/{filename}"')
            if del_ok:
                deleted_count += 1
                logger.info(f"[SMB CLEANUP] DELETED: {filename}")

                # Also delete .notes file if exists
                notes_filename = f"{filename}.notes"
                run_smb_cmd(f'del "{dump_path}/{notes_filename}"')

                # Also delete .log file if exists
                base_name = (
                    filename.replace(".vma.zst", "")
                    .replace(".vma.gz", "")
                    .replace(".vma.lzo", "")
                    .replace(".vma", "")
                    .replace(".tar.zst", "")
                    .replace(".tar.gz", "")
                    .replace(".tar.lzo", "")
                    .replace(".tar", "")
                )
                log_filename = f"{base_name}.log"
                run_smb_cmd(f'del "{dump_path}/{log_filename}"')
                return True
            else:
                logger.warning(f"[SMB CLEANUP] Failed to delete {filename}: {del_out}")
                return False

        total_files = len(filenames_to_delete) + len(timestamp_fallback)
        logger.info(f"[SMB CLEANUP] Starting cleanup for {total_files} backup(s) in {dump_path}")
        logger.info(
            f"[SMB CLEANUP] Direct filenames: {len(filenames_to_delete)}, Timestamp fallback: {len(timestamp_fallback)}"
        )

        deleted_count = 0

        # Method 1: Delete by exact filename (preferred)
        for filename in filenames_to_delete:
            logger.info(f"[SMB CLEANUP] Deleting by filename: {filename}")
            delete_backup_file(filename)

        # Method 2: Fallback to timestamp matching for legacy runs
        if timestamp_fallback:
            logger.info(f"[SMB CLEANUP] Processing {len(timestamp_fallback)} legacy timestamp matches")

            # List files in dump directory for timestamp matching
            # Use cd + ls instead of ls with wildcard to avoid smbclient hanging on quoted paths with spaces
            ok, output = run_smb_cmd(f'cd "{dump_path}"; ls')
            if not ok:
                logger.debug(f"[SMB CLEANUP] Could not list SMB dump path: {output}")
            else:
                for guest_id, timestamp in timestamp_fallback:
                    for line in output.split("\n"):
                        match = re.search(
                            rf"(vzdump-(qemu|lxc)-{guest_id}-(\d{{4}}_\d{{2}}_\d{{2}})-"
                            rf"(\d{{2}}_\d{{2}}_\d{{2}})\.(vma|tar)(?:\.(zst|gz|lzo))?)",
                            line,
                        )
                        if match:
                            filename = match.group(1)
                            file_date = match.group(3)
                            file_time = match.group(4)
                            try:
                                file_dt = datetime.strptime(f"{file_date}_{file_time}", "%Y_%m_%d_%H_%M_%S")
                                timestamp_naive = timestamp.replace(tzinfo=None)
                                time_diff = abs((file_dt - timestamp_naive).total_seconds())
                                # Match if within 10 minutes
                                if time_diff < 600:
                                    logger.info(f"[SMB CLEANUP] Timestamp match (diff: {time_diff:.0f}s): {filename}")
                                    delete_backup_file(filename)
                            except ValueError:
                                continue

        if deleted_count > 0:
            logger.info(f"[SMB CLEANUP] SUCCESS: Deleted {deleted_count} backup files")
        else:
            logger.info("[SMB CLEANUP] No matching backup files found to delete")

    def _cleanup_hypervisor_folder(
        repository: dict[str, Any],
        hypervisor: dict[str, Any],
        storage: Storage,
        repository_id: str,
    ) -> None:
        """Clean up the entire hypervisor folder from a repository.

        This removes the Hypervisors/{hv_name} folder and all contents including:
        - dump/ folder with all backup files
        - .backer/ metadata folder

        Used when deleting a hypervisor to completely remove all traces.
        """
        repo_type = repository.get("repo_type", "").lower()
        server = repository.get("server", "")
        hv_name = hypervisor.get("name", "unknown")

        # Sanitize hypervisor name for folder
        safe_hv_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in hv_name)

        if repo_type == "nfs":
            export = repository.get("share", "")
            if not server or not export:
                logger.warning("Cannot clean up NFS: missing server or export")
                return

            # NFS: backups are at {export}/dump/ (Proxmox puts them at root)
            # Metadata is at {export}/.backer/
            # For hypervisor-specific cleanup, we need to remove hypervisor entries from metadata
            _cleanup_nfs_hypervisor_folder(server, export, safe_hv_name)

        elif repo_type == "smb":
            share = repository.get("share", "")
            subdir = repository.get("path", "").strip("/")
            username = repository.get("username")
            domain = repository.get("domain")
            repo_password = storage.get_storage_password(repository_id)

            if not server or not share:
                logger.warning("Cannot clean up SMB: missing server or share")
                return

            # SMB: hypervisor folder is at {subdir}/Hypervisors/{hv_name}
            hv_folder = f"Hypervisors/{safe_hv_name}"
            if subdir:
                full_hv_path = f"{subdir}/{hv_folder}"
            else:
                full_hv_path = hv_folder

            _cleanup_smb_hypervisor_folder(server, share, username, repo_password, domain, full_hv_path)
        else:
            logger.warning(f"Hypervisor folder cleanup not supported for type: {repo_type}")

    def _cleanup_nfs_hypervisor_folder(
        server: str,
        export: str,
        hypervisor_name: str,
    ) -> None:
        """Clean up hypervisor-specific data from an NFS share.

        For NFS, Proxmox stores backups at the export root in dump/.
        We need to remove hypervisor metadata entries from .backer/.
        """
        import os
        import subprocess
        import tempfile

        logger.info(f"[NFS HV CLEANUP] Cleaning up hypervisor '{hypervisor_name}' from {server}:{export}")

        mount_point = None
        try:
            mount_point = tempfile.mkdtemp(prefix="backer_hv_cleanup_")
            nfs_source = f"{server}:{export}"

            mount_cmd = ["sudo", "-n", "mount", "-t", "nfs", "-o", "rw", nfs_source, mount_point]
            result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                logger.error(f"[NFS HV CLEANUP] Mount failed: {result.stderr.strip()}")
                return

            # Remove hypervisor entry from .backer/hypervisors/
            hypervisors_dir = Path(mount_point) / ".backer" / "hypervisors"
            if hypervisors_dir.exists():
                hv_file = hypervisors_dir / f"{hypervisor_name}.json"
                if hv_file.exists():
                    hv_file.unlink()
                    logger.info(f"[NFS HV CLEANUP] Deleted hypervisor metadata: {hv_file.name}")

            # Remove hypervisor job entries from .backer/hypervisor_jobs/
            jobs_dir = Path(mount_point) / ".backer" / "hypervisor_jobs"
            if jobs_dir.exists():
                for job_file in jobs_dir.glob("*.json"):
                    try:
                        import json

                        data = json.loads(job_file.read_text())
                        # Check if this job belongs to the hypervisor being deleted
                        if (
                            data.get("hypervisor_id") == hypervisor_name
                            or data.get("hypervisor_name") == hypervisor_name
                        ):
                            job_file.unlink()
                            logger.info(f"[NFS HV CLEANUP] Deleted job metadata: {job_file.name}")
                    except Exception as e:
                        logger.warning(f"[NFS HV CLEANUP] Could not check job file {job_file}: {e}")

            # For NFS, backups are shared in dump/ - we've already cleaned up per-job
            # Don't delete the entire dump folder as other hypervisors may use it
            logger.info("[NFS HV CLEANUP] Hypervisor cleanup completed")

        except subprocess.TimeoutExpired:
            logger.error("[NFS HV CLEANUP] NFS mount timed out")
        except Exception as e:
            logger.error(f"[NFS HV CLEANUP] Error: {e}", exc_info=True)
        finally:
            if mount_point:
                try:
                    subprocess.run(["sudo", "-n", "umount", mount_point], capture_output=True, timeout=30)
                    os.rmdir(mount_point)
                except Exception:
                    try:
                        subprocess.run(["sudo", "-n", "umount", "-l", mount_point], capture_output=True, timeout=10)
                        os.rmdir(mount_point)
                    except Exception as e:
                        logger.warning(f"[NFS HV CLEANUP] Could not unmount: {e}")

    def _cleanup_smb_hypervisor_folder(
        server: str,
        share: str,
        username: str | None,
        password: str | None,
        domain: str | None,
        hypervisor_path: str,
    ) -> None:
        """Clean up the entire hypervisor folder from an SMB share.

        This removes the Hypervisors/{hv_name} folder recursively.
        """
        import subprocess

        # Build smbclient auth
        if username:
            if domain:
                auth_str = f"{domain}\\{username}%{password or ''}"
            else:
                auth_str = f"{username}%{password or ''}"
            auth_parts = ["-U", auth_str]
        else:
            auth_parts = ["-N"]

        def run_smb_cmd(cmd: str) -> tuple[bool, str]:
            """Run an smbclient command and return success status and output."""
            full_cmd = ["smbclient", f"//{server}/{share}", *auth_parts, "-c", cmd]
            try:
                result = subprocess.run(full_cmd, capture_output=True, timeout=60, text=True)
                return result.returncode == 0, result.stderr or result.stdout
            except subprocess.TimeoutExpired:
                return False, "Command timed out"
            except Exception as e:
                return False, str(e)

        logger.info(f"[SMB HV CLEANUP] Removing hypervisor folder: {hypervisor_path}")

        # First, recursively delete contents of dump/ folder
        dump_path = f"{hypervisor_path}/dump"
        # Use cd + ls instead of ls with wildcard to avoid smbclient hanging on quoted paths with spaces
        ok, output = run_smb_cmd(f'cd "{dump_path}"; ls')
        if ok:
            # Delete all files in dump/
            for line in output.split("\n"):
                # Extract filename from smbclient ls output
                parts = line.strip().split()
                if parts and not parts[0].startswith("."):
                    filename = parts[0]
                    if filename not in (".", ".."):
                        run_smb_cmd(f'del "{dump_path}/{filename}"')
            # Try to remove the dump directory
            run_smb_cmd(f'rmdir "{dump_path}"')
            logger.info("[SMB HV CLEANUP] Cleaned dump folder")

        # Delete .backer metadata folder contents
        backer_path = f"{hypervisor_path}/.backer"

        # Delete hypervisor_backups subdirs
        hv_backups_path = f"{backer_path}/hypervisor_backups"
        ok, output = run_smb_cmd(f'ls "{hv_backups_path}"')
        if ok:
            for line in output.split("\n"):
                parts = line.strip().split()
                if parts and parts[0] not in (".", "..") and "D" in line:
                    vmid_dir = parts[0]
                    # Delete runs inside
                    runs_path = f"{hv_backups_path}/{vmid_dir}/runs"
                    run_smb_cmd(f'del "{runs_path}/*"')
                    run_smb_cmd(f'rmdir "{runs_path}"')
                    # Delete guest.json
                    run_smb_cmd(f'del "{hv_backups_path}/{vmid_dir}/guest.json"')
                    run_smb_cmd(f'rmdir "{hv_backups_path}/{vmid_dir}"')
            run_smb_cmd(f'rmdir "{hv_backups_path}"')

        # Delete hypervisor_jobs folder
        jobs_path = f"{backer_path}/hypervisor_jobs"
        run_smb_cmd(f'del "{jobs_path}/*"')
        run_smb_cmd(f'rmdir "{jobs_path}"')

        # Delete hypervisors folder
        hypervisors_path = f"{backer_path}/hypervisors"
        run_smb_cmd(f'del "{hypervisors_path}/*"')
        run_smb_cmd(f'rmdir "{hypervisors_path}"')

        # Delete metadata.json and .backer folder
        run_smb_cmd(f'del "{backer_path}/metadata.json"')
        run_smb_cmd(f'rmdir "{backer_path}"')

        # Finally, remove the hypervisor folder itself
        run_smb_cmd(f'rmdir "{hypervisor_path}"')

        logger.info("[SMB HV CLEANUP] Hypervisor folder cleanup completed")

    def _write_metadata_nfs(
        server: str,
        export: str,
        hypervisor_name: str,
        backer_dir: Path,
    ) -> None:
        """Write metadata to NFS share by temporarily mounting it."""
        import subprocess
        import tempfile

        mount_point = tempfile.mkdtemp(prefix="backer_nfs_meta_")

        try:
            # Mount the NFS export
            mount_cmd = [
                "sudo",
                "-n",
                "mount",
                "-t",
                "nfs",
                "-o",
                "soft,timeo=50,retrans=2",
                f"{server}:{export}",
                mount_point,
            ]
            result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                logger.warning(f"Failed to mount NFS for metadata write: {result.stderr.strip()}")
                return

            # Build target path: {mount}/Hypervisors/{hypervisor_name}/.backer
            target_backer = f"{mount_point}/Hypervisors/{hypervisor_name}/.backer"

            # Create directory and copy files using shell commands
            # This handles NFS permissions better than Python's shutil
            try:
                # Create target directory
                mkdir_result = subprocess.run(
                    ["mkdir", "-p", target_backer],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if mkdir_result.returncode != 0:
                    logger.warning(f"Failed to create metadata directory: {mkdir_result.stderr.strip()}")
                    return

                # Copy all files from backer_dir to target using cp -r
                # Use the contents of backer_dir (trailing /.) to copy contents, not the dir itself
                cp_result = subprocess.run(
                    ["cp", "-r", f"{backer_dir}/.", target_backer],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if cp_result.returncode != 0:
                    logger.warning(f"Failed to copy metadata files: {cp_result.stderr.strip()}")
                    return

                logger.info(f"Wrote hypervisor backup metadata to {server}:{export}/Hypervisors/{hypervisor_name}")

            except subprocess.TimeoutExpired:
                logger.warning("Timeout writing metadata files to NFS")

        except subprocess.TimeoutExpired:
            logger.warning(f"Timeout mounting NFS {server}:{export} for metadata")
        finally:
            # Unmount
            try:
                subprocess.run(["sudo", "-n", "umount", mount_point], capture_output=True, timeout=30)
            except Exception:
                try:
                    subprocess.run(["sudo", "-n", "umount", "-l", mount_point], capture_output=True, timeout=10)
                except Exception:
                    pass

            # Remove temp mount point
            try:
                import os

                os.rmdir(mount_point)
            except Exception:
                pass

    def _write_metadata_smb(
        server: str,
        share: str,
        subdir: str,
        hypervisor_name: str,
        username: str | None,
        password: str | None,
        domain: str | None,
        backer_dir: Path,
    ) -> None:
        """Write metadata to SMB share using smbclient."""
        import subprocess

        # Build remote path: {repo_path}/Hypervisors/{hypervisor_name}/
        base_path = subdir.strip("/") if subdir else ""
        if base_path:
            remote_base = f"{base_path}/Hypervisors/{hypervisor_name}"
        else:
            remote_base = f"Hypervisors/{hypervisor_name}"

        # Build smbclient auth
        auth_parts = []
        if username:
            auth_parts.extend(["-U", f"{domain}\\{username}%{password}" if domain else f"{username}%{password}"])
        else:
            auth_parts.extend(["-N"])  # No password

        # Track directories we've already created to avoid redundant mkdir calls
        created_dirs: set[str] = set()

        def ensure_remote_dir(dir_path: str) -> None:
            """Create remote directory and all parents using smbclient."""
            if not dir_path or dir_path in created_dirs:
                return

            # Build list of directories to create (from root to leaf)
            parts = dir_path.split("/")
            for i in range(1, len(parts) + 1):
                partial_path = "/".join(parts[:i])
                if partial_path and partial_path not in created_dirs:
                    mkdir_cmd = ["smbclient", f"//{server}/{share}", *auth_parts, "-c", f"mkdir {partial_path}"]
                    subprocess.run(mkdir_cmd, capture_output=True, timeout=30)
                    created_dirs.add(partial_path)

        # Upload each file in .backer directory
        for local_file in backer_dir.rglob("*"):
            if not local_file.is_file():
                continue

            # rel_path is relative to backer_dir (e.g., "hypervisors/123.json" or "metadata.json")
            rel_path = local_file.relative_to(backer_dir)
            rel_path_str = str(rel_path).replace("\\", "/")

            # Build remote directory path
            # For "metadata.json" -> parent is "." -> remote_dir is just "{remote_base}/.backer"
            # For "hypervisors/123.json" -> remote_dir is "{remote_base}/.backer/hypervisors"
            parent_str = str(rel_path.parent).replace("\\", "/")
            if parent_str == ".":
                remote_dir = f"{remote_base}/.backer"
            else:
                remote_dir = f"{remote_base}/.backer/{parent_str}"

            # Create remote directory (and all parents)
            ensure_remote_dir(remote_dir)

            # Upload file
            remote_file = f"{remote_base}/.backer/{rel_path_str}"
            put_cmd = ["smbclient", f"//{server}/{share}", *auth_parts, "-c", f"put {local_file} {remote_file}"]
            result = subprocess.run(put_cmd, capture_output=True, timeout=30)
            if result.returncode != 0:
                logger.debug(f"smbclient put failed for {remote_file}: {result.stderr.decode()}")
            else:
                logger.debug(f"Uploaded metadata: {remote_file}")

        logger.info(f"Wrote hypervisor backup metadata to //{server}/{share}/{remote_base}")

    def _write_hypervisor_backup_metadata(
        repository: dict[str, Any],
        hypervisor: dict[str, Any],
        job: dict[str, Any],
        job_id: str,
        run_id: str,
        results: list[dict[str, Any]],
        guest_map: dict[int, Any],
    ) -> None:
        """Write backup metadata to the repository.

        This allows the metadata to be discovered if the Backer server is reinstalled.
        Uses smbclient for SMB shares and temporary mount for NFS shares.
        """
        import tempfile

        from backer.hypervisors.metadata import HypervisorMetadata

        repo_type = repository.get("repo_type", "").lower()
        if repo_type not in ("smb", "nfs", "local"):
            logger.debug(f"Skipping metadata write for repo type: {repo_type}")
            return

        # For LOCAL repos, get the local path
        local_path = repository.get("share") or repository.get("path", "")

        # For SMB/NFS repos, get network details
        server = repository.get("server", "")
        share = repository.get("share", "")
        subdir = repository.get("path", "")
        username = repository.get("username")
        password = repository.get("password")
        domain = repository.get("domain")

        # Validate required fields based on repo type
        if repo_type == "local":
            if not local_path:
                logger.warning("Cannot write metadata: missing local path for LOCAL repo")
                return
        elif not server or not share:
            logger.warning("Cannot write metadata: missing server or share")
            return

        # Sanitize hypervisor name for folder
        safe_hv_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in hypervisor["name"])

        # Create metadata in a temp directory first
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            metadata = HypervisorMetadata(tmp_path)

            # Initialize if needed
            if not metadata.is_initialized():
                metadata.initialize()

            # Save hypervisor info
            metadata.save_hypervisor(
                hypervisor_id=hypervisor["id"],
                name=hypervisor["name"],
                hypervisor_type=hypervisor.get("hypervisor_type", "proxmox"),
                host=hypervisor["host"],
            )

            # Save job configuration (for discovery on reinstall)
            metadata.save_job(
                job_id=job_id,
                name=job["name"],
                hypervisor_id=hypervisor["id"],
                repository_id=job.get("repository_id", ""),
                guest_ids=job.get("guest_ids"),
                backup_mode=job.get("backup_mode", "snapshot"),
                compression=job.get("compression", "zstd"),
                schedule_cron=job.get("schedule_cron"),
                enabled=job.get("enabled", True),
                copies_to_keep=job.get("copies_to_keep", 0),
                hypervisor_name=hypervisor["name"],
                hypervisor_host=hypervisor["host"],
            )

            # Save guest and run info for each result
            for result in results:
                vmid = result.get("vmid")
                if not vmid:
                    continue

                guest = guest_map.get(vmid)
                guest_name = guest.name if guest else f"VM {vmid}"
                guest_type = guest.guest_type.value if guest else "qemu"
                node = guest.node if guest else "unknown"

                # Save guest info
                metadata.save_guest(
                    vmid=vmid,
                    name=guest_name,
                    guest_type=guest_type,
                    node=node,
                    hypervisor_id=hypervisor["id"],
                )

                # Save run record
                metadata.save_backup_run(
                    vmid=vmid,
                    run_id=run_id,
                    status="success" if result.get("success") else "failed",
                    backup_file=result.get("archive_name", ""),
                    started_at=result.get("started_at", tz.get_now().isoformat()),
                    finished_at=result.get("finished_at"),
                    size_bytes=result.get("archive_size"),
                    duration_seconds=result.get("duration_seconds"),
                    backup_type=result.get("backup_type", "full"),
                    skipped=result.get("skipped", False),
                    job_name=job["name"],
                    job_id=job_id,
                    hypervisor_id=hypervisor["id"],
                )

            # Now upload the .backer directory to the share/local path
            backer_dir = tmp_path / ".backer"
            if not backer_dir.exists():
                return

            if repo_type == "local":
                # For LOCAL: write directly to filesystem
                _write_metadata_local(
                    local_path=local_path,
                    hypervisor_name=safe_hv_name,
                    backer_dir=backer_dir,
                )
            elif repo_type == "nfs":
                # For NFS: temporarily mount and copy files directly
                _write_metadata_nfs(
                    server=server,
                    export=share,
                    hypervisor_name=safe_hv_name,
                    backer_dir=backer_dir,
                )
            else:
                # For SMB: use smbclient
                _write_metadata_smb(
                    server=server,
                    share=share,
                    subdir=subdir,
                    hypervisor_name=safe_hv_name,
                    username=username,
                    password=password,
                    domain=domain,
                    backer_dir=backer_dir,
                )

    @app.post("/api/v1/hypervisor-jobs/{job_id}/run")
    def run_hypervisor_job(
        job_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Run a hypervisor backup job.

        Backups are stored directly on the Backer repository by auto-configuring
        the repository as Proxmox storage (like Veeam does).

        When copies_to_keep is set (> 0), retention is enforced after each successful
        backup by deleting the oldest backups to stay within the limit.

        Args:
            job_id: Hypervisor job ID
        """
        from backer.hypervisors.proxmox import (
            ProxmoxAPI,
            ProxmoxAPIError,
            ProxmoxAuthMethod,
            ProxmoxBackupManager,
            ProxmoxBackupMode,
            ProxmoxCompression,
        )

        job = storage.get_hypervisor_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        hypervisor = storage.get_hypervisor(job["hypervisor_id"])
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        hypervisor_type = hypervisor.get("hypervisor_type", "proxmox")

        # Dispatch to appropriate handler based on hypervisor type
        if hypervisor_type in ("hyperv", "hyperv-cluster"):
            # Use the scheduler's Hyper-V backup trigger
            repository_id = job.get("repository_id")
            if not repository_id:
                raise HTTPException(status_code=400, detail="No repository configured for this job")

            repository = storage.get_repository(repository_id)
            if not repository:
                raise HTTPException(status_code=404, detail="Repository not found")

            repo_type = repository.get("repo_type", "").lower()
            if repo_type != "smb":
                raise HTTPException(
                    status_code=400,
                    detail=f"Repository type '{repo_type}' is not supported for Hyper-V backups. "
                    "Use an SMB repository so the Hyper-V host can export directly to it.",
                )

            # Trigger backup in background via scheduler mechanism
            if hypervisor_type == "hyperv-cluster":
                _trigger_hyperv_cluster_backup_job(job_id, job, hypervisor)
            else:
                _trigger_hyperv_backup_job(job_id, job, hypervisor)

            return {
                "message": f"Hyper-V backup job '{job['name']}' started",
                "job_id": job_id,
            }

        elif hypervisor_type == "unraid":
            # Use the scheduler's Unraid backup trigger
            repository_id = job.get("repository_id")
            if not repository_id:
                raise HTTPException(status_code=400, detail="No repository configured for this job")

            repository = storage.get_repository(repository_id)
            if not repository:
                raise HTTPException(status_code=404, detail="Repository not found")

            repo_type = repository.get("repo_type", "").lower()
            if repo_type != "smb":
                raise HTTPException(
                    status_code=400,
                    detail=f"Repository type '{repo_type}' is not supported for Unraid backups. Use an SMB repository.",
                )

            # Trigger backup in background via scheduler mechanism
            _trigger_unraid_backup_job(job_id, job, hypervisor)

            return {
                "message": f"Unraid backup job '{job['name']}' started",
                "job_id": job_id,
            }

        # Proxmox backup (original implementation below)
        # Get repository for backup destination
        repository_id = job.get("repository_id")
        if not repository_id:
            raise HTTPException(status_code=400, detail="No repository configured for this job")

        repository = storage.get_repository(repository_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        # Validate repository type (must be SMB or NFS for Proxmox storage)
        repo_type = repository.get("repo_type", "").lower()
        if repo_type not in ("smb", "nfs"):
            raise HTTPException(
                status_code=400,
                detail=f"Repository type '{repo_type}' is not supported for hypervisor backups. "
                "Use an SMB or NFS repository so Proxmox can write directly to it.",
            )

        # Get credentials
        token_secret = storage.get_hypervisor_token_secret(hypervisor["id"])
        hv_password = storage.get_hypervisor_password(hypervisor["id"])

        auth_method = ProxmoxAuthMethod.TOKEN if hypervisor["auth_method"] == "token" else ProxmoxAuthMethod.PASSWORD

        api = ProxmoxAPI(
            host=hypervisor["host"],
            port=hypervisor["port"],
            token_id=hypervisor.get("token_id"),
            token_secret=token_secret,
            username=hypervisor.get("username"),
            password=hv_password,
            auth_method=auth_method,
            verify_ssl=hypervisor.get("verify_ssl", False),
        )

        # Authenticate if using password-based auth
        if auth_method == ProxmoxAuthMethod.PASSWORD:
            api.authenticate()

        # Get repository password for SMB storage
        repo_password = None
        if repo_type == "smb" and repository.get("has_password"):
            repo_password = storage.get_storage_password(repository_id)

        # Create repository dict with password for ensure_backer_storage
        repo_with_password = {**repository, "password": repo_password}

        run_id = tz.get_now().strftime("%Y%m%d_%H%M%S_%f")

        # Get SSH credentials - used for cleanup operations
        ssh_user = hypervisor.get("ssh_user", "root")
        ssh_port = hypervisor.get("ssh_port", 22)
        ssh_key_path = hypervisor.get("ssh_key_path")

        # For SSH password: use API password if ssh_use_api_password is enabled
        ssh_password = None
        if hypervisor.get("ssh_use_api_password", True) and hv_password:
            ssh_password = hv_password

        # Get copies_to_keep setting (0 = unlimited)
        copies_to_keep = job.get("copies_to_keep", 0)
        logger.info(f"Job '{job.get('name')}' copies_to_keep={copies_to_keep}")

        # Submit backup as background task
        def run_backup_task(task: Task) -> dict[str, Any]:
            # Acquire Proxmox storage for this repository (with reference counting)
            # Backups go to: {repo_path}/Hypervisors/{hypervisor_name}/dump/
            task.message = "Configuring backup storage..."
            proxmox_storage_id = None
            try:
                proxmox_storage_id = api.acquire_backer_storage(
                    repo_with_password,
                    hypervisor_name=hypervisor["name"],
                    ssh_user=ssh_user,
                    ssh_port=ssh_port,
                    ssh_key=ssh_key_path,
                    ssh_password=ssh_password,
                )
            except ProxmoxAPIError as e:
                # Raise the error so the task shows as FAILED, not completed
                raise RuntimeError(f"Failed to configure Proxmox storage: {e}") from e

            manager = ProxmoxBackupManager(api)
            results = []

            # Get guest names and resolve guest_ids
            try:
                logger.info("Fetching guest list from Proxmox...")
                all_guests = api.list_guests()
                guest_map = {g.vmid: g for g in all_guests}
                logger.info(f"Found {len(all_guests)} guests: {[g.vmid for g in all_guests]}")
            except Exception as e:
                logger.warning(f"Failed to list guests: {e}")
                all_guests = []
                guest_map = {}

            # If no specific guests, backup all
            guest_ids = job.get("guest_ids") or []
            logger.info(f"Job guest_ids from storage: {guest_ids} (types: {[type(x).__name__ for x in guest_ids]})")
            if not guest_ids:
                guest_ids = [g.vmid for g in all_guests]
                logger.info(f"No specific guests, backing up all: {guest_ids}")

            total = len(guest_ids)
            logger.info(f"Total guests to backup: {total}")
            if total == 0:
                logger.warning("No guests to backup - returning early")
                return {"run_id": run_id, "total": 0, "success": 0, "failed": 0, "results": []}

            for i, vmid in enumerate(guest_ids):
                logger.info(f"=== Processing guest {i + 1}/{total}: VMID {vmid} ===")
                task.progress = int((i / total) * 100)
                guest = guest_map.get(vmid)
                guest_name = guest.name if guest else f"VM {vmid}"
                guest_type = guest.guest_type.value if guest else "qemu"
                vmid_type = type(vmid).__name__
                logger.info(f"Guest lookup: vmid={vmid} ({vmid_type}), found={guest is not None}")

                task.message = f"Backing up {guest_name} ({vmid}) to {repository['name']}..."

                # Record pending
                storage.save_hypervisor_run(
                    run_id=run_id,
                    job_id=job_id,
                    job_name=job["name"],
                    hypervisor_id=hypervisor["id"],
                    guest_id=vmid,
                    guest_name=guest_name,
                    guest_type=guest_type,
                    status="running",
                )

                try:
                    # Map backup mode
                    mode_map = {
                        "snapshot": ProxmoxBackupMode.SNAPSHOT,
                        "stop": ProxmoxBackupMode.STOP,
                        "suspend": ProxmoxBackupMode.SUSPEND,
                    }
                    backup_mode = mode_map.get(job["backup_mode"], ProxmoxBackupMode.SNAPSHOT)

                    # Map compression
                    compress_map = {
                        "zstd": ProxmoxCompression.ZSTD,
                        "gzip": ProxmoxCompression.GZIP,
                        "lzo": ProxmoxCompression.LZO,
                        "none": ProxmoxCompression.NONE,
                    }
                    compression = compress_map.get(job["compression"], ProxmoxCompression.ZSTD)

                    # Progress callback to update task with vzdump progress
                    def progress_callback(status: Any, log_lines: list[str]) -> None:
                        import re

                        for line in log_lines:
                            # Parse vzdump progress: "INFO: 5% (1.2G of 24G)"
                            match = re.search(r"(\d+)%", line)
                            if match:
                                pct = int(match.group(1))
                                # Scale to this VM's portion of overall progress
                                base_progress = int((i / total) * 100)
                                vm_progress = int((pct / 100) * (100 / total))
                                task.progress = min(base_progress + vm_progress, 99)
                            # Also update message with latest log line
                            if "INFO:" in line:
                                short_line = line.replace("INFO:", "").strip()[:60]
                                task.message = f"Backing up {guest_name}: {short_line}"

                    # Backup directly to Proxmox storage (which points to Backer repo)
                    result = manager.backup_to_storage(
                        vmid=vmid,
                        storage=proxmox_storage_id,
                        mode=backup_mode,
                        compress=compression,
                        retention=job.get("retention"),
                        timeout=7200,
                        progress_callback=progress_callback,
                    )

                    # Add backup type to result
                    result["backup_type"] = "full"
                    backup_size = result.get("backup_size", 0)
                    backup_filename = result.get("backup_filename")

                    # Update run record
                    storage.save_hypervisor_run(
                        run_id=run_id,
                        job_id=job_id,
                        job_name=job["name"],
                        hypervisor_id=hypervisor["id"],
                        guest_id=vmid,
                        guest_name=guest_name,
                        guest_type=guest_type,
                        status="success" if result["success"] else "failed",
                        upid=result.get("upid"),
                        finished_at=datetime.fromisoformat(result["finished_at"]),
                        duration_seconds=result.get("duration_seconds"),
                        backup_size=backup_size,
                        backup_filename=backup_filename,
                        exit_status=result.get("exit_status"),
                        errors=result.get("errors"),
                    )

                    # Enforce copies_to_keep retention after successful backup
                    if result["success"] and copies_to_keep > 0:
                        task.message = f"Enforcing retention for {guest_name} ({vmid})..."
                        logger.info(f"Enforcing copies_to_keep={copies_to_keep} for VM {vmid}")
                        try:
                            _enforce_copies_limit(
                                repository=repo_with_password,
                                hypervisor_name=hypervisor["name"],
                                vmid=vmid,
                                copies_to_keep=copies_to_keep,
                            )
                        except Exception as e:
                            logger.warning(f"Failed to enforce retention for VM {vmid}: {e}")
                            # Don't fail backup if retention enforcement fails

                    results.append(result)

                except Exception as e:
                    logger.exception(f"Backup failed for VMID {vmid}: {e}")

                    storage.save_hypervisor_run(
                        run_id=run_id,
                        job_id=job_id,
                        job_name=job["name"],
                        hypervisor_id=hypervisor["id"],
                        guest_id=vmid,
                        guest_name=guest_name,
                        guest_type=guest_type,
                        status="failed",
                        finished_at=tz.get_now(),
                        errors=[str(e)],
                    )
                    results.append({"success": False, "vmid": vmid, "error": str(e)})

            task.progress = 100
            task.message = "Backup job completed"

            # Write metadata to repository
            try:
                task.message = "Writing backup metadata..."
                _write_hypervisor_backup_metadata(
                    repository=repo_with_password,
                    hypervisor=hypervisor,
                    job=job,
                    job_id=job_id,
                    run_id=run_id,
                    results=results,
                    guest_map=guest_map,
                )
            except Exception as meta_err:
                # Don't fail backup if metadata write fails
                logger.warning(f"Failed to write backup metadata: {meta_err}")

            # Release storage reference (only deletes if no other tasks using it)
            # This unmounts the share when last task completes, keeping Proxmox UI clean
            if proxmox_storage_id:
                task.message = "Cleaning up temporary storage..."
                deleted = api.release_backer_storage(proxmox_storage_id)
                if deleted:
                    logger.info(f"Removed temporary Proxmox storage '{proxmox_storage_id}'")

            success_count = sum(1 for r in results if r.get("success"))
            failed_count = sum(1 for r in results if not r.get("success"))
            return {
                "run_id": run_id,
                "total": total,
                "success": success_count,
                "failed": failed_count,
                "results": results,
            }

        task_manager = get_task_manager()
        guest_count = len(job.get("guest_ids") or [])
        hv_name = hypervisor["name"]
        repo_name = repository["name"]
        if guest_count:
            desc = f"Backing up {guest_count} guests from {hv_name} to {repo_name}"
        else:
            desc = f"Backing up all guests from {hv_name} to {repo_name}"

        task = task_manager.submit(
            task_type="hypervisor_backup",
            description=desc,
            func=run_backup_task,
        )

        msg = f"Backup job started for {guest_count} guests" if guest_count else "Backup job started for all guests"
        return {
            "run_id": run_id,
            "task_id": task.id,
            "message": msg,
        }

    @app.patch("/api/v1/hypervisor-jobs/{job_id}/toggle")
    def toggle_hypervisor_job(
        job_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Toggle a hypervisor job's enabled state."""
        job = storage.get_hypervisor_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        new_enabled = not job["enabled"]
        storage.update_hypervisor_job(job_id, enabled=new_enabled)

        return {"id": job_id, "enabled": new_enabled}

    @app.get("/api/v1/hypervisor-jobs/{job_id}/runs")
    def get_hypervisor_job_runs(
        job_id: str,
        limit: int = 50,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """Get backup runs for a hypervisor job."""
        job = storage.get_hypervisor_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        return storage.get_hypervisor_runs(job_id=job_id, limit=limit)

    @app.get("/api/v1/hypervisor-runs/{run_id}")
    def get_hypervisor_run(
        run_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Get details of a specific hypervisor backup run."""
        run = storage.get_hypervisor_run_by_run_id(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return run

    # ============ Incremental Backup Status ============

    @app.get("/api/v1/hypervisors/{hypervisor_id}/incremental-status/{vmid}")
    def get_vm_incremental_status(
        hypervisor_id: str,
        vmid: int,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Get incremental backup status for a specific VM.

        Returns bitmap tracking state, dirty bytes, and backup history.
        """
        from backer.hypervisors.incremental import IncrementalBackupManager

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        # Get SSH credentials
        ssh_password = None
        if hypervisor.get("ssh_use_api_password", True):
            ssh_password = storage.get_hypervisor_password(hypervisor_id)

        inc_manager = IncrementalBackupManager(
            host=hypervisor["host"],
            hypervisor_id=hypervisor_id,
            storage=storage,
            ssh_user=hypervisor.get("ssh_user", "root"),
            ssh_port=hypervisor.get("ssh_port", 22),
            ssh_key=hypervisor.get("ssh_key_path"),
            ssh_password=ssh_password,
        )

        try:
            stats = inc_manager.get_vm_backup_stats(vmid)
            validity = inc_manager.check_bitmap_validity(vmid)
            return {
                "vmid": vmid,
                "stats": stats,
                "validity": validity,
            }
        except Exception as e:
            logger.warning(f"Error getting incremental status for VM {vmid}: {e}")
            return {
                "vmid": vmid,
                "error": str(e),
                "stats": {"tracked": False},
                "validity": {"valid": False, "reason": str(e)},
            }

    @app.post("/api/v1/hypervisors/{hypervisor_id}/incremental-setup/{vmid}")
    def setup_vm_incremental_tracking(
        hypervisor_id: str,
        vmid: int,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Set up incremental backup tracking for a VM.

        Creates dirty bitmaps on all VM disks to enable change tracking.
        Should be called after a full backup or when re-initializing tracking.
        """
        from backer.hypervisors.incremental import IncrementalBackupManager

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        # Get SSH credentials
        ssh_password = None
        if hypervisor.get("ssh_use_api_password", True):
            ssh_password = storage.get_hypervisor_password(hypervisor_id)

        inc_manager = IncrementalBackupManager(
            host=hypervisor["host"],
            hypervisor_id=hypervisor_id,
            storage=storage,
            ssh_user=hypervisor.get("ssh_user", "root"),
            ssh_port=hypervisor.get("ssh_port", 22),
            ssh_key=hypervisor.get("ssh_key_path"),
            ssh_password=ssh_password,
        )

        try:
            result = inc_manager.setup_tracking(vmid)
            return result
        except Exception as e:
            logger.error(f"Error setting up incremental tracking for VM {vmid}: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to set up incremental tracking: {e}")

    @app.delete("/api/v1/hypervisors/{hypervisor_id}/incremental-cleanup/{vmid}")
    def cleanup_vm_incremental_tracking(
        hypervisor_id: str,
        vmid: int,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Remove incremental backup tracking for a VM.

        Removes dirty bitmaps and clears tracking state from the database.
        """
        from backer.hypervisors.qmp import QMPClient, QMPError

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        # Get SSH credentials
        ssh_password = None
        if hypervisor.get("ssh_use_api_password", True):
            ssh_password = storage.get_hypervisor_password(hypervisor_id)

        # Try to clean up bitmaps on the VM
        bitmaps_removed = 0
        try:
            client = QMPClient(
                host=hypervisor["host"],
                vmid=vmid,
                ssh_user=hypervisor.get("ssh_user", "root"),
                ssh_port=hypervisor.get("ssh_port", 22),
                ssh_key=hypervisor.get("ssh_key_path"),
                ssh_password=ssh_password,
            )

            if client.is_vm_running():
                bitmaps_removed = client.cleanup_bitmaps()

        except QMPError as e:
            logger.warning(f"Could not clean up bitmaps on VM {vmid}: {e}")

        # Clean up database state
        db_removed = storage.delete_vm_bitmap_state(hypervisor_id, vmid)

        return {
            "vmid": vmid,
            "bitmaps_removed": bitmaps_removed,
            "db_records_removed": db_removed,
        }

    # ============ Hypervisor Backups ============

    def _get_hyperv_job_backups(
        job: dict,
        hypervisor: dict,
        repository: dict,
        repo_password: str | None,
        storage: Storage,
        job_guest_ids: list,
    ) -> list[dict[str, Any]]:
        """Get Hyper-V backups by querying the Windows host via WinRM.

        Hyper-V backups use a VM-centric folder structure:
        {backup_path}/{vm_name}/{timestamp}/{vm_name}/Virtual Machines/*.vmcx
        """
        from backer.hypervisors.hyperv import (
            HyperVAPI,
            HyperVBackupManager,
            HyperVClusterAPI,
            HyperVClusterBackupManager,
        )

        repo_type = repository.get("repo_type", "").lower()
        if repo_type != "smb":
            logger.warning(f"Hyper-V backups require SMB repository, got: {repo_type}")
            return []

        # Get Hyper-V credentials
        hv_password = storage.get_hypervisor_password(hypervisor["id"])
        hypervisor_type = hypervisor.get("hypervisor_type", "hyperv")

        try:
            # Get domain from hypervisor data
            hv_domain = hypervisor.get("domain") or hypervisor.get("config", {}).get("domain")

            # Use cluster API for hyperv-cluster type
            if hypervisor_type == "hyperv-cluster":
                cluster_name = hypervisor.get("cluster_name") or hypervisor.get("config", {}).get("cluster_name")
                api = HyperVClusterAPI(
                    host=hypervisor["host"],
                    username=hypervisor.get("username", "Administrator"),
                    password=hv_password,
                    port=hypervisor.get("port", 5985),
                    use_ssl=hypervisor.get("port", 5985) == 5986,
                    verify_ssl=hypervisor.get("verify_ssl", False),
                    domain=hv_domain,
                    cluster_name=cluster_name,
                )
                backup_manager = HyperVClusterBackupManager(api)
            else:
                api = HyperVAPI(
                    host=hypervisor["host"],
                    username=hypervisor.get("username", "Administrator"),
                    password=hv_password,
                    port=hypervisor.get("port", 5985),
                    use_ssl=hypervisor.get("port", 5985) == 5986,
                    verify_ssl=hypervisor.get("verify_ssl", False),
                    domain=hv_domain,
                )
                backup_manager = HyperVBackupManager(api)

            # Build UNC path to backups
            smb_server = repository.get("server", "")
            smb_share = repository.get("share", "")
            smb_path = repository.get("path", "")

            backup_base_path = f"\\\\{smb_server}\\{smb_share}"
            if smb_path:
                smb_path_win = smb_path.replace("/", "\\").strip("\\")
                backup_base_path = f"{backup_base_path}\\{smb_path_win}"

            # Add hypervisor subfolder
            safe_hv_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in hypervisor["name"])
            backup_base_path = f"{backup_base_path}\\Hypervisors\\{safe_hv_name}"

            # Get SMB credentials from repository
            smb_username = repository.get("username", "")
            smb_domain = repository.get("domain")

            # List backups via WinRM
            backups = backup_manager.list_backups(
                backup_path=backup_base_path,
                smb_username=smb_username,
                smb_password=repo_password,
                smb_domain=smb_domain,
            )

            # Convert to the format expected by the frontend
            logger.info(f"Raw backups from WinRM: {backups}")
            result = []
            for backup in backups:
                vm_name = backup.get("vm_name", "")
                timestamp = backup.get("timestamp", "")

                # Parse created_at to get ctime
                created_at = backup.get("created_at", "")
                ctime = 0
                if created_at:
                    try:
                        # Handle ISO format with timezone
                        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        ctime = dt.timestamp()
                    except ValueError:
                        pass

                result.append(
                    {
                        "filename": f"{vm_name}/{timestamp}",
                        "volid": backup.get("path", ""),
                        "vmid": vm_name,  # Hyper-V uses VM name as ID
                        "vm_name": vm_name,
                        "guest_type": "vm",
                        "ctime": ctime,
                        "size": backup.get("size_bytes", 0),
                        "format": "vmcx",
                        "node": hypervisor.get("name", "unknown"),
                        "path": backup.get("path", ""),
                        "timestamp": timestamp,
                        "vmcx_file": backup.get("vmcx_file", ""),
                    }
                )

            # Filter by job's guest_ids if specified (for Hyper-V, these are VM names or GUIDs)
            logger.info(f"Before filter: {len(result)} backups, job_guest_ids={job_guest_ids}")
            if job_guest_ids:
                # Convert guest_ids to lowercase strings for comparison
                guest_id_strs = [str(gid).lower() for gid in job_guest_ids]

                # Build a mapping of GUID -> VM name by listing VMs from the hypervisor
                # This allows us to match backups by VM name when job stores GUIDs
                guid_to_name: dict[str, str] = {}
                name_to_guid: dict[str, str] = {}
                try:
                    guests = api.list_guests()
                    for g in guests:
                        if g.vmid and g.name:
                            guid_to_name[g.vmid.lower()] = g.name.lower()
                            name_to_guid[g.name.lower()] = g.vmid.lower()
                    logger.debug(f"Built GUID->name map with {len(guid_to_name)} entries")
                except Exception as e:
                    logger.warning(f"Could not build GUID->name map: {e}")

                # Expand guest_id_strs to include VM names for any GUIDs
                expanded_ids = set(guest_id_strs)
                for gid in guest_id_strs:
                    if gid in guid_to_name:
                        expanded_ids.add(guid_to_name[gid])
                    if gid in name_to_guid:
                        expanded_ids.add(name_to_guid[gid])

                logger.info(f"Filtering by expanded guest_ids={expanded_ids}")

                def backup_matches(b: dict) -> bool:
                    """Check if backup matches any of the job's guest IDs."""
                    vm_name = b.get("vm_name", "").lower()
                    vmid = str(b.get("vmid", "")).lower() if b.get("vmid") else ""
                    # Also check vmcx_file which contains the VM GUID
                    vmcx = b.get("vmcx_file", "").lower().replace(".vmcx", "")
                    return vm_name in expanded_ids or vmid in expanded_ids or vmcx in expanded_ids

                result = [b for b in result if backup_matches(b)]
                logger.info(f"After filter: {len(result)} backups")

            # Sort by ctime descending (newest first)
            result.sort(key=lambda x: x.get("ctime", 0), reverse=True)

            logger.info(f"Returning {len(result)} Hyper-V backups to frontend")
            return result[:50]  # Limit to 50 most recent

        except Exception as e:
            logger.warning(f"Error listing Hyper-V backups: {e}", exc_info=True)
            return []

    @app.get("/api/v1/hypervisors/{hypervisor_id}/backups")
    def list_hypervisor_backups(
        hypervisor_id: str,
        node: str | None = None,
        storage_id: str | None = None,
        vmid: int | None = None,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List backups on a hypervisor storage.

        If node and storage_id are not provided, scans all backup-capable
        storages across all nodes.

        Dispatches to hypervisor-specific implementations based on type.
        """
        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        hypervisor_type = hypervisor.get("hypervisor_type", "proxmox")

        # Dispatch to type-specific implementations
        if hypervisor_type in ("hyperv", "hyperv-cluster"):
            # Hyper-V backups are stored in repositories, not on the hypervisor
            # Aggregate backups from all jobs associated with this hypervisor
            all_backups = []
            jobs = storage.list_hypervisor_jobs()
            for job in jobs:
                if job.get("hypervisor_id") != hypervisor_id:
                    continue
                repository_id = job.get("repository_id")
                if not repository_id:
                    continue
                repository = storage.get_repository(repository_id)
                if not repository:
                    continue
                repo_password = storage.get_storage_password(repository_id)
                try:
                    job_backups = _get_hyperv_job_backups(
                        job=job,
                        hypervisor=hypervisor,
                        repository=repository,
                        repo_password=repo_password,
                        storage=storage,
                        job_guest_ids=[],  # Get all backups, not filtered
                    )
                    all_backups.extend(job_backups)
                except Exception as e:
                    logger.warning(f"Error listing backups for job {job.get('name')}: {e}")
            # Sort by ctime descending and deduplicate by path
            seen_paths: set[str] = set()
            unique_backups = []
            for b in sorted(all_backups, key=lambda x: x.get("ctime", 0), reverse=True):
                path = b.get("path", "")
                if path and path not in seen_paths:
                    seen_paths.add(path)
                    unique_backups.append(b)
            return unique_backups[:50]

        elif hypervisor_type == "unraid":
            # Unraid needs a path to list backups - return empty if not provided
            return []

        # Proxmox implementation
        from backer.hypervisors.proxmox import ProxmoxAPI, ProxmoxAuthMethod

        token_secret = storage.get_hypervisor_token_secret(hypervisor_id)
        password = storage.get_hypervisor_password(hypervisor_id)

        auth_method = ProxmoxAuthMethod.TOKEN if hypervisor["auth_method"] == "token" else ProxmoxAuthMethod.PASSWORD

        api = ProxmoxAPI(
            host=hypervisor["host"],
            port=hypervisor["port"],
            token_id=hypervisor.get("token_id"),
            token_secret=token_secret,
            username=hypervisor.get("username"),
            password=password,
            auth_method=auth_method,
            verify_ssl=hypervisor.get("verify_ssl", False),
        )

        try:
            # Authenticate if using password-based auth
            if auth_method == ProxmoxAuthMethod.PASSWORD:
                api.authenticate()

            all_backups = []
            seen_volids: set[str] = set()  # Deduplicate for shared storage in clusters

            if node and storage_id:
                # Specific node and storage
                backups = api.list_backups(node, storage_id, vmid)
                for backup in backups:
                    if backup.volid not in seen_volids:
                        seen_volids.add(backup.volid)
                        all_backups.append(backup)
            else:
                # Scan all backup-capable storages across all nodes
                storages = api.list_storages()
                nodes = api.list_nodes()

                for pve_node in nodes:
                    for pve_storage in storages:
                        # Skip if storage not on this node
                        if pve_storage.node and pve_storage.node != pve_node.node:
                            continue
                        try:
                            backups = api.list_backups(pve_node.node, pve_storage.storage, vmid)
                            for backup in backups:
                                # Deduplicate - shared storage shows same backups on all nodes
                                if backup.volid not in seen_volids:
                                    seen_volids.add(backup.volid)
                                    all_backups.append(backup)
                        except Exception:
                            # Storage might not be accessible on this node
                            pass

            # Sort by ctime descending (newest first)
            all_backups.sort(key=lambda b: b.ctime, reverse=True)

            return [
                {
                    "volid": b.volid,
                    "vmid": b.vmid,
                    "node": b.node,
                    "ctime": b.ctime.timestamp(),
                    "size": b.size,
                    "format": b.format,
                    "notes": b.notes,
                    "protected": b.protected,
                    "guest_type": b.guest_type,  # qemu or lxc
                }
                for b in all_backups[:50]  # Limit to 50 most recent
            ]
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/v1/hypervisor-jobs/{job_id}/backups")
    def list_hypervisor_job_backups(
        job_id: str,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List backups for a hypervisor job from repository metadata.

        This reads the backup metadata stored in the repository, which persists
        even if the Backer server is reinstalled. This is the source of truth
        for what backups exist in the repository.
        """
        import subprocess

        job = storage.get_hypervisor_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        repository_id = job.get("repository_id")
        if not repository_id:
            return []

        repository = storage.get_repository(repository_id)
        if not repository:
            return []

        hypervisor = storage.get_hypervisor(job["hypervisor_id"])
        if not hypervisor:
            return []

        # Get job's guest_ids to filter results (empty list means all guests)
        job_guest_ids = job.get("guest_ids") or []

        repo_type = repository.get("repo_type", "").lower()
        server = repository.get("server", "")
        share = repository.get("share", "")
        subdir = repository.get("path", "")
        username = repository.get("username")
        domain = repository.get("domain")

        # Get repository password
        repo_password = storage.get_storage_password(repository_id)

        # Check hypervisor type for type-specific backup listing
        hypervisor_type = hypervisor.get("hypervisor_type", "proxmox")
        logger.info(f"list_hypervisor_job_backups: job={job_id}, hypervisor_type={hypervisor_type}")

        # Handle Hyper-V backups (different folder structure)
        if hypervisor_type in ("hyperv", "hyperv-cluster"):
            logger.info(f"Calling _get_hyperv_job_backups for {hypervisor_type}")
            return _get_hyperv_job_backups(
                job=job,
                hypervisor=hypervisor,
                repository=repository,
                repo_password=repo_password,
                storage=storage,
                job_guest_ids=job_guest_ids,
            )

        # Handle NFS repositories
        if repo_type == "nfs" and server:
            nfs_export = repository.get("share") or repository.get("path", "")
            if nfs_export:
                backups = _get_job_backups_from_nfs(
                    server=server,
                    export=nfs_export,
                    hypervisor_name=hypervisor["name"],
                    storage=storage,
                    job_id=job_id,
                )
                # Filter by job's guest_ids if specified
                if job_guest_ids:
                    backups = [b for b in backups if b.get("vmid") in job_guest_ids]
                return backups

        if repo_type != "smb" or not server or not share:
            # For unsupported repo types, fall back to local storage runs
            return _get_job_backups_from_local_storage(storage, job_id)

        # Build the path to metadata: {repo_path}/Hypervisors/{hypervisor_name}/.backer/
        base_path = subdir.strip("/") if subdir else ""
        safe_hv_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in hypervisor["name"])
        if base_path:
            remote_base = f"{base_path}/Hypervisors/{safe_hv_name}"
        else:
            remote_base = f"Hypervisors/{safe_hv_name}"

        # Build smbclient auth
        auth_parts = []
        if username:
            pw = repo_password or ""
            if domain:
                auth_parts.extend(["-U", f"{domain}\\{username}%{pw}"])
            else:
                auth_parts.extend(["-U", f"{username}%{pw}"])
        else:
            auth_parts.extend(["-N"])

        # List vzdump files in dump/ directory
        dump_path = f"{remote_base}/dump"
        logger.debug(f"Listing backups from //{server}/{share}/{dump_path}")

        list_cmd = ["smbclient", f"//{server}/{share}", *auth_parts, "-c", f"ls {dump_path}/vzdump-*"]

        try:
            result = subprocess.run(list_cmd, capture_output=True, timeout=30, text=True)
            if result.returncode != 0:
                logger.debug(f"smbclient ls dump/ failed (trying without dump/): {result.stderr}")
                # Try without dump/ prefix (older structure)
                list_cmd[-1] = f"ls {remote_base}/vzdump-*"
                result = subprocess.run(list_cmd, capture_output=True, timeout=30, text=True)

            backups = []
            if result.returncode == 0 and result.stdout:
                # Parse smbclient ls output
                # Format: "  vzdump-qemu-100-2025_12_06-10_30_00.vma.zst      A   123456789  Fri Dec  6 10:35:00 2025"
                # Or:     "  vzdump-qemu-100-2025_12_06-10_30_00.vma.zst  N  123456789  Fri Dec  6 10:35:00 2025"
                import re

                for line in result.stdout.split("\n"):
                    # Skip empty lines and summary lines
                    if not line.strip() or "blocks of size" in line or "blocks available" in line:
                        continue

                    # Extract vzdump filename from line
                    # smbclient format: "  filename  ATTR  SIZE  DATE"
                    # QEMU VMs use .vma extension, LXC containers use .tar extension
                    vzdump_match = re.search(
                        r"(vzdump-(qemu|lxc)-(\d+)-(\d{4}_\d{2}_\d{2})-(\d{2}_\d{2}_\d{2})\.(vma|tar)(?:\.(zst|gz|lzo))?)",
                        line,
                    )
                    if vzdump_match:
                        filename = vzdump_match.group(1)
                        guest_type = vzdump_match.group(2)
                        vmid = vzdump_match.group(3)
                        date_str = vzdump_match.group(4)
                        time_str = vzdump_match.group(5)
                        # group 6 is now the extension (vma|tar), compression is group 7
                        compression = vzdump_match.group(7)

                        # Try to extract size from the line (digits followed by date)
                        size_match = re.search(r"\s(\d+)\s+\w{3}\s+\w{3}", line)
                        size = int(size_match.group(1)) if size_match else 0

                        try:
                            backup_time = datetime.strptime(f"{date_str}_{time_str}", "%Y_%m_%d_%H_%M_%S")
                            backups.append(
                                {
                                    "filename": filename,
                                    "volid": f"backer:{dump_path}/{filename}",
                                    "vmid": int(vmid),
                                    "guest_type": guest_type,
                                    "ctime": backup_time.timestamp(),
                                    "size": size,
                                    "format": "vma",
                                    "node": hypervisor.get("name", "unknown"),
                                    "compression": compression or "none",
                                }
                            )
                        except ValueError as e:
                            logger.debug(f"Failed to parse backup date from {filename}: {e}")

            # Filter by job's guest_ids if specified
            if job_guest_ids:
                backups = [b for b in backups if b.get("vmid") in job_guest_ids]

            # Sort by time descending
            backups.sort(key=lambda x: x.get("ctime", 0), reverse=True)
            return backups[:50]

        except subprocess.TimeoutExpired:
            logger.warning("Timeout listing backups from SMB share")
            fallback = _get_job_backups_from_local_storage(storage, job_id)
            if job_guest_ids:
                fallback = [b for b in fallback if b.get("vmid") in job_guest_ids]
            return fallback
        except Exception as e:
            logger.warning(f"Error listing backups from SMB: {e}")
            fallback = _get_job_backups_from_local_storage(storage, job_id)
            if job_guest_ids:
                fallback = [b for b in fallback if b.get("vmid") in job_guest_ids]
            return fallback

    def _get_job_backups_from_nfs(
        server: str,
        export: str,
        hypervisor_name: str,
        storage: Storage,
        job_id: str,
    ) -> list[dict[str, Any]]:
        """List vzdump backups from an NFS share by temporarily mounting it."""
        import re
        import subprocess
        import tempfile

        mount_point = tempfile.mkdtemp(prefix="backer_nfs_list_")
        backups: list[dict[str, Any]] = []
        mounted = False

        try:
            # Mount the NFS export
            mount_cmd = [
                "sudo",
                "-n",
                "mount",
                "-t",
                "nfs",
                "-o",
                "soft,timeo=50,retrans=2,ro",  # read-only, soft mount
                f"{server}:{export}",
                mount_point,
            ]
            result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                logger.warning(f"Failed to mount NFS for backup listing: {result.stderr.strip()}")
                return _get_job_backups_from_local_storage(storage, job_id)

            mounted = True

            # List vzdump files in dump/ directory (where Proxmox stores backups)
            dump_path = Path(mount_point) / "dump"

            if dump_path.exists() and dump_path.is_dir():
                # Collect entries first to avoid issues if NFS becomes unresponsive during iteration
                try:
                    entries = list(dump_path.iterdir())
                except OSError as e:
                    logger.warning(f"Failed to list NFS directory: {e}")
                    entries = []  # Continue to finally block for cleanup

                for entry in entries:
                    if not entry.name.startswith("vzdump-"):
                        continue

                    # Parse vzdump filename: vzdump-qemu-100-2025_12_07-15_09_27.vma.zst
                    # QEMU VMs use .vma extension, LXC containers use .tar extension
                    vzdump_match = re.match(
                        r"vzdump-(qemu|lxc)-(\d+)-(\d{4}_\d{2}_\d{2})-(\d{2}_\d{2}_\d{2})\.(vma|tar)(?:\.(zst|gz|lzo))?$",
                        entry.name,
                    )
                    if vzdump_match:
                        guest_type = vzdump_match.group(1)
                        vmid = vzdump_match.group(2)
                        date_str = vzdump_match.group(3)
                        time_str = vzdump_match.group(4)
                        file_format = vzdump_match.group(5)  # vma or tar
                        compression = vzdump_match.group(6)

                        try:
                            # Get file size (may fail on stale NFS)
                            try:
                                size = entry.stat().st_size
                            except OSError:
                                size = 0

                            # Parse backup time from filename
                            backup_time = datetime.strptime(f"{date_str}_{time_str}", "%Y_%m_%d_%H_%M_%S")

                            backups.append(
                                {
                                    "filename": entry.name,
                                    "volid": f"backup/{entry.name}",
                                    "vmid": int(vmid),
                                    "guest_type": guest_type,
                                    "ctime": backup_time.timestamp(),
                                    "size": size,
                                    "format": file_format,
                                    "node": hypervisor_name,
                                    "compression": compression or "none",
                                }
                            )
                        except ValueError as e:
                            logger.debug(f"Failed to parse backup {entry.name}: {e}")

            # Sort by time descending
            backups.sort(key=lambda x: x.get("ctime", 0), reverse=True)

        except subprocess.TimeoutExpired:
            logger.warning(f"Timeout mounting NFS {server}:{export} for backup listing")
            return _get_job_backups_from_local_storage(storage, job_id)
        except Exception as e:
            logger.warning(f"Error listing backups from NFS: {e}")
            return _get_job_backups_from_local_storage(storage, job_id)
        finally:
            # Unmount and cleanup - always try even if mount failed
            if mounted:
                try:
                    subprocess.run(
                        ["sudo", "-n", "umount", "-l", mount_point],  # lazy unmount for reliability
                        capture_output=True,
                        timeout=30,
                    )
                except Exception:
                    pass
            try:
                Path(mount_point).rmdir()
            except Exception:
                pass

        return backups[:50]

    def _get_job_backups_from_local_storage(storage: Storage, job_id: str) -> list[dict[str, Any]]:
        """Get backup history from local database for a hypervisor job."""
        runs = storage.get_hypervisor_runs(job_id=job_id, limit=100)
        backups = []
        for run in runs:
            if run.get("status") == "success":
                # Convert started_at to timestamp
                started_at = run.get("started_at")
                if isinstance(started_at, str):
                    try:
                        started_at = datetime.fromisoformat(started_at).timestamp()
                    except ValueError:
                        started_at = 0
                elif isinstance(started_at, datetime):
                    started_at = started_at.timestamp()
                else:
                    started_at = 0

                backups.append(
                    {
                        "filename": f"vzdump-qemu-{run.get('guest_id', 0)}-backup",
                        "volid": run.get("upid", ""),
                        "vmid": run.get("guest_id", 0),
                        "guest_type": "qemu",  # Default
                        "ctime": started_at,
                        "size": 0,  # Not tracked in run table
                        "format": "vma",
                        "node": run.get("guest_name", "unknown"),
                    }
                )
        return backups

    @app.post("/api/v1/hypervisors/{hypervisor_id}/restore")
    async def restore_hypervisor_backup(
        hypervisor_id: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Restore a VM/container from backup."""
        from backer.hypervisors.proxmox import (
            ProxmoxAPI,
            ProxmoxAuthMethod,
            ProxmoxBackupManager,
            ProxmoxGuestType,
        )

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        body = await request.json()

        archive = body.get("archive")
        node = body.get("node")
        guest_type = body.get("guest_type", "qemu")
        target_vmid = body.get("vmid")  # Optional: restore to different VMID
        target_storage = body.get("storage")
        force = body.get("force", False)
        start_after = body.get("start", False)
        unique = body.get("unique", False)  # Regenerate MAC addresses

        if not archive or not node:
            raise HTTPException(status_code=400, detail="archive and node are required")

        # Parse original VMID from backup filename (e.g., vzdump-qemu-100-2024...)
        import re

        vmid_match = re.search(r"vzdump-(?:qemu|lxc)-(\d+)-", archive)
        if not vmid_match:
            raise HTTPException(status_code=400, detail="Could not parse VMID from backup filename")
        original_vmid = int(vmid_match.group(1))

        # Use target_vmid if provided, otherwise restore to original VMID
        vmid = target_vmid or original_vmid

        token_secret = storage.get_hypervisor_token_secret(hypervisor_id)
        password = storage.get_hypervisor_password(hypervisor_id)

        auth_method = ProxmoxAuthMethod.TOKEN if hypervisor["auth_method"] == "token" else ProxmoxAuthMethod.PASSWORD

        api = ProxmoxAPI(
            host=hypervisor["host"],
            port=hypervisor["port"],
            token_id=hypervisor.get("token_id"),
            token_secret=token_secret,
            username=hypervisor.get("username"),
            password=password,
            auth_method=auth_method,
            verify_ssl=hypervisor.get("verify_ssl", False),
        )

        # Authenticate if using password-based auth
        if auth_method == ProxmoxAuthMethod.PASSWORD:
            api.authenticate()

        # Submit restore as background task
        def run_restore_task(task: Task) -> dict[str, Any]:
            manager = ProxmoxBackupManager(api)
            task.message = f"Restoring to VMID {vmid} from backup..."

            try:
                result = manager.restore_guest(
                    vmid=original_vmid,  # Original VMID from backup
                    archive=archive,
                    node=node,
                    guest_type=ProxmoxGuestType.QEMU if guest_type == "qemu" else ProxmoxGuestType.LXC,
                    target_vmid=vmid,  # Target VMID (may be same or different)
                    storage=target_storage,
                    force=force,
                    start_after=start_after,
                    unique=unique,
                )

                task.progress = 100
                task.message = "Restore completed" if result["success"] else "Restore failed"
                return result

            except Exception as e:
                return {"success": False, "error": str(e)}

        task_manager = get_task_manager()
        task = task_manager.submit(
            task_type="hypervisor_restore",
            description=f"Restoring VMID {vmid} on {hypervisor['name']}",
            func=run_restore_task,
        )

        return {
            "task_id": task.id,
            "message": f"Restore started for VMID {vmid}",
        }

    async def _restore_hyperv_from_repository(
        job: dict,
        hypervisor: dict,
        repository: dict,
        body: dict,
        storage: Storage,
    ) -> dict[str, Any]:
        """Restore a Hyper-V VM from a backup in the repository."""
        from backer.hypervisors.hyperv import HyperVAPI, HyperVBackupManager

        filename = body.get("filename")  # e.g., "testwin11/20251211_123556"
        new_vm_name = body.get("vmid")  # For Hyper-V, vmid field is used as new VM name
        # Handle case where vmid is sent as null or empty string
        if new_vm_name in (None, "", "null"):
            new_vm_name = None
        start_after = body.get("start", False)
        # Get restore mode from request (auto, inplace, import, rebuild)
        restore_mode = body.get("restore_mode", "auto")
        if restore_mode not in ("auto", "inplace", "import", "rebuild"):
            restore_mode = "auto"
        # Get target node for cluster restore (optional)
        target_node = body.get("target_node")
        if target_node in (None, "", "null"):
            target_node = None

        if not filename:
            raise HTTPException(status_code=400, detail="filename is required")

        # Get credentials
        hv_password = storage.get_hypervisor_password(hypervisor["id"])
        repo_password = storage.get_storage_password(repository["id"])

        # Get domain from hypervisor
        hv_domain = hypervisor.get("domain") or hypervisor.get("config", {}).get("domain")
        hypervisor_type = hypervisor.get("hypervisor_type", "hyperv")

        # Use cluster API for hyperv-cluster type
        if hypervisor_type == "hyperv-cluster":
            from backer.hypervisors.hyperv import HyperVClusterAPI, HyperVClusterBackupManager

            cluster_name = hypervisor.get("cluster_name") or hypervisor.get("config", {}).get("cluster_name")
            api = HyperVClusterAPI(
                host=hypervisor["host"],
                username=hypervisor.get("username", "Administrator"),
                password=hv_password,
                cluster_name=cluster_name,
                port=hypervisor.get("port", 5985),
                use_ssl=hypervisor.get("port", 5985) == 5986,
                verify_ssl=hypervisor.get("verify_ssl", False),
                domain=hv_domain,
            )
            backup_manager = HyperVClusterBackupManager(api)
        else:
            api = HyperVAPI(
                host=hypervisor["host"],
                username=hypervisor.get("username", "Administrator"),
                password=hv_password,
                port=hypervisor.get("port", 5985),
                use_ssl=hypervisor.get("port", 5985) == 5986,
                verify_ssl=hypervisor.get("verify_ssl", False),
                domain=hv_domain,
            )
            backup_manager = HyperVBackupManager(api)

        # Store info needed for GUID lookup in background task
        hypervisor_id = hypervisor.get("id")

        # Extract VM name from filename (e.g., "testwin11/20251211_123556" -> "testwin11")
        parts = filename.replace("/", "\\").split("\\")
        vm_name_from_path = parts[0] if parts else None

        # Get the job's current guest_ids - one of these is the current GUID for this VM
        job_guest_ids = job.get("guest_ids", [])

        # Build UNC path to backup
        smb_server = repository.get("server", "")
        smb_share = repository.get("share", "")
        smb_path = repository.get("path", "")

        backup_base_path = f"\\\\{smb_server}\\{smb_share}"
        if smb_path:
            smb_path_win = smb_path.replace("/", "\\").strip("\\")
            backup_base_path = f"{backup_base_path}\\{smb_path_win}"

        # Add hypervisor subfolder
        safe_hv_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in hypervisor["name"])
        backup_base_path = f"{backup_base_path}\\Hypervisors\\{safe_hv_name}"

        # Build full path to the backup
        # filename is like "testwin11/20251211_123556"
        # Full path: \\server\share\path\Hypervisors\hvname\testwin11\20251211_123556\testwin11
        parts = filename.replace("/", "\\").split("\\")
        if len(parts) >= 2:
            vm_name = parts[0]
            timestamp = parts[1]
            import_path = f"{backup_base_path}\\{vm_name}\\{timestamp}\\{vm_name}"
        else:
            import_path = f"{backup_base_path}\\{filename}"
            vm_name = filename

        # Get SMB credentials from repository
        smb_username = repository.get("username", "")
        smb_domain = repository.get("domain")

        logger.info(f"Starting Hyper-V restore from {import_path}")

        def run_hyperv_restore(task: Task) -> dict[str, Any]:
            try:
                task.message = f"Preparing restore for {vm_name_from_path or 'VM'}..."
                task.progress = 5

                # Determine old_vm_id for GUID update (runs in background to avoid blocking GUI)
                # This finds the current GUID in the job that corresponds to this VM
                old_vm_id = None
                try:
                    all_guests = backup_manager.list_all_guests()
                    guest_by_id = {g["vmid"].lower(): g for g in all_guests}
                    guest_by_name = {g["name"].lower(): g for g in all_guests}

                    # First try: find a VM in the job that matches our VM name
                    for gid in job_guest_ids:
                        guest = guest_by_id.get(gid.lower())
                        if guest and vm_name_from_path and guest["name"].lower() == vm_name_from_path.lower():
                            old_vm_id = gid.lower()
                            logger.info(
                                f"Found current VM GUID from job: {old_vm_id} (matches VM name '{vm_name_from_path}')"
                            )
                            break

                    # Second try: if VM doesn't exist yet (deleted), the job still has the GUID
                    # In this case, we can't match by name, so we check if job only has one guest_id
                    if not old_vm_id and len(job_guest_ids) == 1:
                        # Single-VM job - use its guest_id
                        old_vm_id = job_guest_ids[0].lower()
                        logger.info(f"Using single guest_id from job as old_vm_id: {old_vm_id}")
                    elif not old_vm_id and vm_name_from_path:
                        # Try to find by VM name in the current VM list
                        guest = guest_by_name.get(vm_name_from_path.lower())
                        if guest and guest["vmid"].lower() in [gid.lower() for gid in job_guest_ids]:
                            old_vm_id = guest["vmid"].lower()
                            logger.info(f"Found current VM GUID by name lookup: {old_vm_id}")
                except Exception as e:
                    logger.warning(f"Could not determine current VM GUID from job: {e}")

                task.message = f"Restoring VM from {import_path}"
                task.progress = 10

                # Build kwargs for restore - include target_node for cluster restores
                restore_kwargs = {
                    "import_path": import_path,
                    "vm_name": new_vm_name if new_vm_name else None,
                    "generate_new_id": True,
                    "smb_username": smb_username,
                    "smb_password": repo_password,
                    "smb_domain": smb_domain,
                    "restore_mode": restore_mode,
                    "start_after_restore": start_after,
                }
                # Add target_node for cluster restores
                if target_node:
                    restore_kwargs["target_node"] = target_node

                result = backup_manager.restore_vm(**restore_kwargs)

                if result.get("success"):
                    restored_name = result.get("vm_name", vm_name)
                    new_vm_id = result.get("vm_id")  # New GUID after restore
                    task.message = f"VM '{restored_name}' restored successfully"
                    task.progress = 90

                    # Log GUID info for debugging
                    logger.info(
                        f"GUID update check: old_vm_id={old_vm_id!r}, new_vm_id={new_vm_id!r}, "
                        f"will_update={bool(new_vm_id and old_vm_id)}"
                    )

                    # Update backup jobs: replace old GUID with new GUID in guest_ids
                    # This handles the case where restore creates a new VM with a new GUID
                    if new_vm_id and old_vm_id:
                        try:
                            logger.info(
                                f"Updating backup jobs: replacing old GUID {old_vm_id} "
                                f"with new GUID {new_vm_id} for VM '{restored_name}'"
                            )
                            # Get all hypervisor jobs for this hypervisor
                            all_jobs = _storage.list_hypervisor_jobs()
                            updated_job_count = 0
                            for job in all_jobs:
                                if job.get("hypervisor_id") == hypervisor_id:
                                    guest_ids = job.get("guest_ids", [])
                                    # Replace old GUID with new GUID (case-insensitive comparison)
                                    # Check if old_vm_id matches any guest_id (case-insensitive)
                                    # Convert to strings first to handle both int and str VMIDs
                                    guest_ids_lower = [str(gid).lower() for gid in guest_ids]
                                    if old_vm_id.lower() in guest_ids_lower:
                                        updated_guest_ids = [
                                            new_vm_id if str(gid).lower() == old_vm_id.lower() else gid
                                            for gid in guest_ids
                                        ]
                                        _storage.update_hypervisor_job(job["id"], guest_ids=updated_guest_ids)
                                        logger.info(
                                            f"Updated job '{job['name']}': "
                                            f"guest_ids changed from {guest_ids} to {updated_guest_ids}"
                                        )
                                        updated_job_count += 1

                            if updated_job_count > 0:
                                logger.info(f"Successfully updated {updated_job_count} backup job(s) with new GUID")
                            else:
                                logger.warning(
                                    f"No backup jobs contained old GUID {old_vm_id} - "
                                    f"this VM may not be in any backup jobs"
                                )
                        except Exception as e:
                            logger.warning(f"Failed to update backup jobs with new GUID: {e}")

                    # Build success response with warnings if any
                    response = {
                        "success": True,
                        "vm_name": restored_name,
                        "vm_id": new_vm_id,
                        "message": f"VM '{restored_name}' restored successfully",
                        "restore_mode": result.get("actual_mode", "unknown"),
                        "config_applied": result.get("config_loaded", False),
                    }
                    if result.get("warnings"):
                        response["warnings"] = result["warnings"]
                    return response
                else:
                    errors = result.get("errors", ["Unknown error"])
                    raise Exception(f"Restore failed: {errors}")
            except Exception as e:
                logger.exception(f"Hyper-V restore failed: {e}")
                raise

        # Run restore as background task
        task_mgr = get_task_manager()
        task = task_mgr.submit(
            task_type="hyperv_restore",
            description=f"Restoring Hyper-V VM from {filename}",
            func=run_hyperv_restore,
        )

        return {
            "task_id": task.id,
            "message": f"Hyper-V restore started for {vm_name}",
        }

    @app.post("/api/v1/hypervisor-jobs/{job_id}/restore")
    async def restore_from_repository(
        job_id: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Restore a VM/container from a backup stored in the Backer repository.

        This endpoint:
        1. Temporarily mounts the repository as Proxmox storage
        2. Performs the restore via Proxmox API
        3. Cleans up the temporary storage mount
        """
        from backer.hypervisors.proxmox import (
            ProxmoxAPI,
            ProxmoxAPIError,
            ProxmoxAuthMethod,
            ProxmoxBackupManager,
            ProxmoxGuestType,
        )

        job = storage.get_hypervisor_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        hypervisor_id = job["hypervisor_id"]
        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        repository_id = job["repository_id"]
        repository = storage.get_repository(repository_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        body = await request.json()

        # Check hypervisor type - Hyper-V has different restore flow
        hypervisor_type = hypervisor.get("hypervisor_type", "proxmox")
        if hypervisor_type in ("hyperv", "hyperv-cluster"):
            return await _restore_hyperv_from_repository(
                job=job,
                hypervisor=hypervisor,
                repository=repository,
                body=body,
                storage=storage,
            )

        filename = body.get("filename")  # e.g., "vzdump-qemu-100-2025_12_07-10_04_54.vma.zst"
        target_vmid = body.get("vmid")  # Optional: restore to different VMID
        target_storage = body.get("storage")  # Optional: storage for VM disks
        force = body.get("force", False)
        start_after = body.get("start", False)
        guest_type = body.get("guest_type", "qemu")
        unique = body.get("unique", False)  # Regenerate MAC addresses

        if not filename:
            raise HTTPException(status_code=400, detail="filename is required")

        # Parse original VMID from backup filename
        import re

        vmid_match = re.search(r"vzdump-(?:qemu|lxc)-(\d+)-", filename)
        if not vmid_match:
            raise HTTPException(status_code=400, detail="Could not parse VMID from backup filename")
        original_vmid = int(vmid_match.group(1))

        # Detect guest type from filename if not specified
        if "vzdump-lxc-" in filename:
            guest_type = "lxc"

        # Use target_vmid if provided, otherwise restore to original VMID
        vmid = target_vmid or original_vmid

        # Get credentials
        token_secret = storage.get_hypervisor_token_secret(hypervisor_id)
        hv_password = storage.get_hypervisor_password(hypervisor_id)
        repo_password = None
        if repository.get("repo_type") == "smb" and repository.get("has_password"):
            repo_password = storage.get_storage_password(repository_id)

        auth_method = ProxmoxAuthMethod.TOKEN if hypervisor["auth_method"] == "token" else ProxmoxAuthMethod.PASSWORD

        api = ProxmoxAPI(
            host=hypervisor["host"],
            port=hypervisor["port"],
            token_id=hypervisor.get("token_id"),
            token_secret=token_secret,
            username=hypervisor.get("username"),
            password=hv_password,
            auth_method=auth_method,
            verify_ssl=hypervisor.get("verify_ssl", False),
        )

        if auth_method == ProxmoxAuthMethod.PASSWORD:
            api.authenticate()

        # Get SSH credentials for storage cleanup
        ssh_user = job.get("ssh_user") or hypervisor.get("ssh_user", "root")
        ssh_port = job.get("ssh_port") or hypervisor.get("ssh_port", 22)
        ssh_key_path = hypervisor.get("ssh_key_path")
        ssh_password = None
        if hypervisor.get("ssh_use_api_password", True) and hv_password:
            ssh_password = hv_password

        # Build repository dict with password
        repo_with_password = {**repository, "password": repo_password}

        def run_restore_task(task: Task) -> dict[str, Any]:
            proxmox_storage_id = None
            try:
                # Step 1: Acquire repository as Proxmox storage (with reference counting)
                task.message = "Mounting backup repository..."
                task.progress = 10
                try:
                    proxmox_storage_id = api.acquire_backer_storage(
                        repo_with_password,
                        hypervisor_name=hypervisor["name"],
                        ssh_user=ssh_user,
                        ssh_port=ssh_port,
                        ssh_key=ssh_key_path,
                        ssh_password=ssh_password,
                    )
                except ProxmoxAPIError as e:
                    raise RuntimeError(f"Failed to mount repository: {e}") from e

                # Step 2: Find which node has the storage active
                task.message = "Finding target node..."
                task.progress = 20

                # Wait for storage to be active and get the node where it's mounted
                max_wait = 30
                poll_interval = 2
                waited = 0
                target_node = None

                while waited < max_wait:
                    is_active, active_node = api.is_storage_active_on_any_node(proxmox_storage_id)
                    if is_active and active_node:
                        target_node = active_node
                        logger.info(f"Storage '{proxmox_storage_id}' is active on node '{target_node}'")
                        break
                    logger.info(f"Waiting for storage to become active... ({waited}s)")
                    time.sleep(poll_interval)
                    waited += poll_interval

                if not target_node:
                    raise RuntimeError(
                        f"Storage '{proxmox_storage_id}' not active on any node after {max_wait}s. "
                        "Cannot perform restore."
                    )

                # Step 3: Build the archive volid
                # Format: "storage_id:backup/filename"
                archive = f"{proxmox_storage_id}:backup/{filename}"

                task.message = f"Restoring VMID {vmid} from {filename}..."
                task.progress = 30

                # Step 4: Perform restore
                manager = ProxmoxBackupManager(api)

                def progress_callback(status: Any, log_lines: list[str]) -> None:
                    # Update progress based on log lines
                    for line in log_lines:
                        if "extracting" in line.lower():
                            task.progress = min(task.progress + 5, 90)
                        if "INFO:" in line:
                            short_line = line.replace("INFO:", "").strip()[:60]
                            task.message = f"Restoring: {short_line}"

                result = manager.restore_guest(
                    vmid=original_vmid,
                    archive=archive,
                    node=target_node,
                    guest_type=ProxmoxGuestType.QEMU if guest_type == "qemu" else ProxmoxGuestType.LXC,
                    target_vmid=vmid,
                    storage=target_storage,
                    force=force,
                    start_after=start_after,
                    unique=unique,
                    progress_callback=progress_callback,
                )

                task.progress = 95
                task.message = "Cleaning up..."

                return result

            except Exception as e:
                logger.exception(f"Restore failed: {e}")
                return {"success": False, "error": str(e)}

            finally:
                # Step 5: Release storage reference (only deletes if no other tasks using it)
                if proxmox_storage_id:
                    try:
                        deleted = api.release_backer_storage(proxmox_storage_id)
                        if deleted:
                            logger.info(f"Cleaned up temporary Proxmox storage '{proxmox_storage_id}'")
                    except Exception as cleanup_err:
                        logger.warning(f"Failed to cleanup storage '{proxmox_storage_id}': {cleanup_err}")

        task_manager = get_task_manager()
        task = task_manager.submit(
            task_type="hypervisor_restore",
            description=f"Restoring VMID {vmid} from {filename}",
            func=run_restore_task,
        )

        return {
            "task_id": task.id,
            "message": f"Restore started for VMID {vmid}",
            "filename": filename,
        }

    # ============ Unraid Restore API ============

    @app.post("/api/v1/hypervisors/{hypervisor_id}/unraid-restore")
    async def restore_unraid_backup(
        hypervisor_id: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Restore an Unraid VM or container appdata from backup.

        Supports restoring:
        - VMs: Restores disk files and XML configuration
        - Docker appdata: Restores container data directory
        """
        from backer.hypervisors.unraid import UnraidAPI, UnraidBackupManager

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        if hypervisor.get("hypervisor_type") != "unraid":
            raise HTTPException(status_code=400, detail="This endpoint is only for Unraid hypervisors")

        body = await request.json()

        # Required
        backup_path = body.get("backup_path")
        restore_type = body.get("type", "vm")  # vm or docker

        if not backup_path:
            raise HTTPException(status_code=400, detail="backup_path is required")

        # Type-specific options
        vm_name = body.get("vm_name")  # For VM restores
        container_name = body.get("container_name")  # For Docker restores
        restore_path = body.get("restore_path")  # Custom destination
        start_after = body.get("start", False)
        stop_container = body.get("stop_container", True)

        # Get credentials
        token_secret = storage.get_hypervisor_token_secret(hypervisor_id)
        password = storage.get_hypervisor_password(hypervisor_id)
        api_key = token_secret or password

        if not api_key:
            raise HTTPException(status_code=400, detail="API key not configured")

        port = hypervisor.get("port", 443)
        api = UnraidAPI(
            host=hypervisor["host"],
            api_key=api_key,
            port=port,
            use_https=port in (443, 8443),
            verify_ssl=hypervisor.get("verify_ssl", False),
        )

        # Get SSH settings
        ssh_password = password if hypervisor.get("ssh_use_api_password", True) else None

        manager = UnraidBackupManager(
            api=api,
            ssh_host=hypervisor["host"],
            ssh_user=hypervisor.get("ssh_user", "root"),
            ssh_port=hypervisor.get("ssh_port", 22),
            ssh_key_path=hypervisor.get("ssh_key_path"),
            ssh_password=ssh_password,
        )

        def run_restore_task(task: Task) -> dict[str, Any]:
            def progress_callback(status: dict[str, Any]) -> None:
                task.message = status.get("status", "Restoring...")
                if "progress" in status:
                    task.progress = status["progress"]

            try:
                if restore_type == "vm":
                    task.message = f"Restoring VM from {backup_path}..."
                    result = manager.restore_vm(
                        backup_path=backup_path,
                        vm_name=vm_name,
                        restore_path=restore_path,
                        start_after=start_after,
                        progress_callback=progress_callback,
                    )
                elif restore_type == "docker":
                    if not container_name:
                        return {"success": False, "errors": ["container_name is required for docker restores"]}
                    task.message = f"Restoring appdata for {container_name}..."
                    result = manager.restore_appdata(
                        backup_path=backup_path,
                        container_name=container_name,
                        restore_path=restore_path,
                        stop_container=stop_container,
                        progress_callback=progress_callback,
                    )
                else:
                    return {"success": False, "errors": [f"Unknown restore type: {restore_type}"]}

                task.progress = 100
                task.message = "Restore completed" if result.get("success") else "Restore failed"
                return result

            except Exception as e:
                logger.exception(f"Unraid restore failed: {e}")
                return {"success": False, "errors": [str(e)]}

        task_manager = get_task_manager()
        task = task_manager.submit(
            task_type="hypervisor_restore",
            description=f"Restoring {restore_type} from {backup_path}",
            func=run_restore_task,
        )

        return {
            "task_id": task.id,
            "message": f"Restore started from {backup_path}",
        }

    @app.get("/api/v1/hypervisors/{hypervisor_id}/unraid-backups")
    async def list_unraid_backups(
        hypervisor_id: str,
        path: str,
        backup_type: str | None = None,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List available Unraid backups in a directory."""
        from backer.hypervisors.unraid import UnraidAPI, UnraidBackupManager

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        if hypervisor.get("hypervisor_type") != "unraid":
            raise HTTPException(status_code=400, detail="This endpoint is only for Unraid hypervisors")

        token_secret = storage.get_hypervisor_token_secret(hypervisor_id)
        password = storage.get_hypervisor_password(hypervisor_id)
        api_key = token_secret or password

        if not api_key:
            raise HTTPException(status_code=400, detail="API key not configured")

        port = hypervisor.get("port", 443)
        api = UnraidAPI(
            host=hypervisor["host"],
            api_key=api_key,
            port=port,
            use_https=port in (443, 8443),
            verify_ssl=hypervisor.get("verify_ssl", False),
        )

        ssh_password = password if hypervisor.get("ssh_use_api_password", True) else None

        manager = UnraidBackupManager(
            api=api,
            ssh_host=hypervisor["host"],
            ssh_user=hypervisor.get("ssh_user", "root"),
            ssh_port=hypervisor.get("ssh_port", 22),
            ssh_key_path=hypervisor.get("ssh_key_path"),
            ssh_password=ssh_password,
        )

        return manager.list_backups(path, backup_type)

    # ============ Hyper-V Restore API ============

    @app.post("/api/v1/hypervisors/{hypervisor_id}/hyperv-restore")
    async def restore_hyperv_backup(
        hypervisor_id: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Restore a Hyper-V VM from an export backup.

        This endpoint imports a VM from a previously exported backup folder.
        The backup must be accessible from the Hyper-V host (typically via SMB/UNC path).

        For SMB paths, provide repository_id to use the repository's SMB credentials
        for authentication when accessing the backup files.
        """
        from backer.hypervisors.hyperv import HyperVAPI, HyperVBackupManager

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        if hypervisor.get("hypervisor_type") not in ("hyperv", "hyperv-cluster"):
            raise HTTPException(status_code=400, detail="This endpoint is only for Hyper-V hypervisors")

        body = await request.json()

        # Required: path to the exported VM folder
        import_path = body.get("import_path")
        if not import_path:
            raise HTTPException(status_code=400, detail="import_path is required")

        # Optional parameters
        vm_name = body.get("vm_name")  # New name for the VM
        restore_path = body.get("restore_path")  # Where to store VM files
        vhd_destination_path = body.get("vhd_destination_path")  # Where to store VHDs
        generate_new_id = body.get("generate_new_id", True)  # Generate new VM ID
        start_after = body.get("start", False)  # Start VM after restore
        repository_id = body.get("repository_id")  # Repository for SMB credentials

        # Get Hyper-V credentials
        password = storage.get_hypervisor_password(hypervisor_id)
        if not password:
            raise HTTPException(status_code=400, detail="Hypervisor password not configured")

        # Get domain from hypervisor data or config
        domain = hypervisor.get("domain") or hypervisor.get("config", {}).get("domain")

        # Get SMB credentials from repository if provided
        smb_username: str | None = None
        smb_password: str | None = None
        smb_domain: str | None = None

        if repository_id:
            repository = storage.get_repository(repository_id)
            if repository and repository.get("repo_type") == "smb":
                smb_username = repository.get("username", "")
                smb_password = storage.get_storage_password(repository_id)
                smb_domain = repository.get("domain", "")

        api = HyperVAPI(
            host=hypervisor["host"],
            username=hypervisor.get("username", "Administrator"),
            password=password,
            port=hypervisor.get("port", 5985),
            use_ssl=hypervisor.get("port", 5985) == 5986,
            verify_ssl=hypervisor.get("verify_ssl", False),
            domain=domain,
        )

        def run_restore_task(task: Task) -> dict[str, Any]:
            manager = HyperVBackupManager(api)
            task.message = f"Importing VM from {import_path}..."
            task.progress = 10

            def progress_callback(status: dict[str, Any]) -> None:
                if status.get("status") == "importing":
                    task.message = f"Importing from {status.get('path', import_path)}..."
                    task.progress = 30
                elif status.get("status") == "completed":
                    task.progress = 90
                    task.message = "Import completed"

            try:
                result = manager.restore_vm(
                    import_path=import_path,
                    vm_name=vm_name,
                    restore_path=restore_path,
                    vhd_destination_path=vhd_destination_path,
                    generate_new_id=generate_new_id,
                    progress_callback=progress_callback,
                    smb_username=smb_username,
                    smb_password=smb_password,
                    smb_domain=smb_domain,
                    restore_mode="auto",  # Use auto mode for intelligent restore
                    start_after_restore=start_after,  # Let restore_vm handle VM start
                )

                task.progress = 100
                task.message = "Restore completed" if result.get("success") else "Restore failed"
                return result

            except Exception as e:
                logger.exception(f"Hyper-V restore failed: {e}")
                return {"success": False, "errors": [str(e)]}

        task_manager = get_task_manager()
        task = task_manager.submit(
            task_type="hypervisor_restore",
            description=f"Restoring Hyper-V VM from {import_path}",
            func=run_restore_task,
        )

        return {
            "task_id": task.id,
            "message": f"Restore started from {import_path}",
        }

    @app.post("/api/v1/hypervisors/{hypervisor_id}/hyperv-restore/preflight")
    async def preflight_hyperv_restore(
        hypervisor_id: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Run preflight checks before Hyper-V VM restore.

        Validates all prerequisites are met before attempting a restore:
        - Source backup exists and is accessible
        - VHD files exist and are readable
        - vm_full_config.json is loadable
        - Target storage has sufficient free space
        - Required virtual switches exist on the host
        - SMB/network connectivity is working
        """
        from backer.hypervisors.hyperv import HyperVAPI, HyperVBackupManager

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        if hypervisor.get("hypervisor_type") not in ("hyperv", "hyperv-cluster"):
            raise HTTPException(status_code=400, detail="This endpoint is only for Hyper-V hypervisors")

        body = await request.json()
        import_path = body.get("import_path")
        job_id = body.get("job_id")
        filename = body.get("filename")

        # Support both direct import_path and job_id + filename approach
        repository_id = body.get("repository_id")
        smb_username: str | None = None
        smb_password: str | None = None
        smb_domain: str | None = None

        if job_id and filename and not import_path:
            # Build import_path from job and filename
            job = storage.get_hypervisor_job(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            repository_id = job.get("repository_id")
            if not repository_id:
                raise HTTPException(status_code=400, detail="Job has no repository configured")
            repository = storage.get_repository(repository_id)
            if not repository:
                raise HTTPException(status_code=404, detail="Repository not found")

            # Build full UNC path to backup (same logic as restore endpoint)
            smb_server = repository.get("server", "")
            smb_share = repository.get("share", "")
            smb_path = repository.get("path", "")

            backup_base_path = f"\\\\{smb_server}\\{smb_share}"
            if smb_path:
                smb_path_win = smb_path.replace("/", "\\").strip("\\")
                backup_base_path = f"{backup_base_path}\\{smb_path_win}"

            # Add hypervisor subfolder
            safe_hv_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in hypervisor["name"])
            backup_base_path = f"{backup_base_path}\\Hypervisors\\{safe_hv_name}"

            # Build full path to the backup
            # filename is like "vmname/timestamp"
            # Full path: \\server\share\path\Hypervisors\hvname\vmname\timestamp\vmname
            parts = filename.replace("/", "\\").split("\\")
            if len(parts) >= 2:
                vm_name_from_path = parts[0]
                timestamp = parts[1]
                import_path = f"{backup_base_path}\\{vm_name_from_path}\\{timestamp}\\{vm_name_from_path}"
            else:
                import_path = f"{backup_base_path}\\{filename}"

        if not import_path:
            raise HTTPException(status_code=400, detail="import_path or (job_id + filename) is required")

        vm_name = body.get("vm_name")
        restore_path = body.get("restore_path")
        vhd_destination_path = body.get("vhd_destination_path")

        password = storage.get_hypervisor_password(hypervisor_id)
        if not password:
            raise HTTPException(status_code=400, detail="Hypervisor password not configured")

        domain = hypervisor.get("domain") or hypervisor.get("config", {}).get("domain")

        if repository_id:
            repository = storage.get_repository(repository_id)
            if repository and repository.get("repo_type") == "smb":
                smb_username = repository.get("username", "")
                smb_password = storage.get_storage_password(repository_id)
                smb_domain = repository.get("domain", "")

        api = HyperVAPI(
            host=hypervisor["host"],
            username=hypervisor.get("username", "Administrator"),
            password=password,
            port=hypervisor.get("port", 5985),
            use_ssl=hypervisor.get("port", 5985) == 5986,
            verify_ssl=hypervisor.get("verify_ssl", False),
            domain=domain,
        )

        manager = HyperVBackupManager(api)
        result = manager.preflight_restore(
            import_path=import_path,
            vm_name=vm_name,
            restore_path=restore_path,
            vhd_destination_path=vhd_destination_path,
            smb_username=smb_username,
            smb_password=smb_password,
            smb_domain=smb_domain,
        )

        return result

    @app.post("/api/v1/hypervisors/{hypervisor_id}/hyperv-restore/dry-run")
    async def dry_run_hyperv_restore(
        hypervisor_id: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Perform a dry-run restore simulation.

        Shows exactly what the restore would do without executing:
        - Files that would be copied
        - VM settings that would be applied
        - Switches required (and which are missing)
        - Estimated duration and space needed
        """
        from backer.hypervisors.hyperv import HyperVAPI, HyperVBackupManager

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        if hypervisor.get("hypervisor_type") not in ("hyperv", "hyperv-cluster"):
            raise HTTPException(status_code=400, detail="This endpoint is only for Hyper-V hypervisors")

        body = await request.json()
        import_path = body.get("import_path")
        job_id = body.get("job_id")
        filename = body.get("filename")

        # Support both direct import_path and job_id + filename approach
        repository_id = body.get("repository_id")
        smb_username: str | None = None
        smb_password: str | None = None
        smb_domain: str | None = None

        if job_id and filename and not import_path:
            # Build import_path from job and filename
            job = storage.get_hypervisor_job(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            repository_id = job.get("repository_id")
            if not repository_id:
                raise HTTPException(status_code=400, detail="Job has no repository configured")
            repository = storage.get_repository(repository_id)
            if not repository:
                raise HTTPException(status_code=404, detail="Repository not found")

            # Build full UNC path to backup (same logic as restore endpoint)
            smb_server = repository.get("server", "")
            smb_share = repository.get("share", "")
            smb_path = repository.get("path", "")

            backup_base_path = f"\\\\{smb_server}\\{smb_share}"
            if smb_path:
                smb_path_win = smb_path.replace("/", "\\").strip("\\")
                backup_base_path = f"{backup_base_path}\\{smb_path_win}"

            # Add hypervisor subfolder
            safe_hv_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in hypervisor["name"])
            backup_base_path = f"{backup_base_path}\\Hypervisors\\{safe_hv_name}"

            # Build full path to the backup
            # filename is like "vmname/timestamp"
            # Full path: \\server\share\path\Hypervisors\hvname\vmname\timestamp\vmname
            parts = filename.replace("/", "\\").split("\\")
            if len(parts) >= 2:
                vm_name_from_path = parts[0]
                timestamp = parts[1]
                import_path = f"{backup_base_path}\\{vm_name_from_path}\\{timestamp}\\{vm_name_from_path}"
            else:
                import_path = f"{backup_base_path}\\{filename}"

        if not import_path:
            raise HTTPException(status_code=400, detail="import_path or (job_id + filename) is required")

        vm_name = body.get("vm_name")
        restore_path = body.get("restore_path")
        vhd_destination_path = body.get("vhd_destination_path")
        network_mapping = body.get("network_mapping")
        start_after = body.get("start_after", body.get("start", False))

        password = storage.get_hypervisor_password(hypervisor_id)
        if not password:
            raise HTTPException(status_code=400, detail="Hypervisor password not configured")

        domain = hypervisor.get("domain") or hypervisor.get("config", {}).get("domain")

        if repository_id:
            repository = storage.get_repository(repository_id)
            if repository and repository.get("repo_type") == "smb":
                smb_username = repository.get("username", "")
                smb_password = storage.get_storage_password(repository_id)
                smb_domain = repository.get("domain", "")

        api = HyperVAPI(
            host=hypervisor["host"],
            username=hypervisor.get("username", "Administrator"),
            password=password,
            port=hypervisor.get("port", 5985),
            use_ssl=hypervisor.get("port", 5985) == 5986,
            verify_ssl=hypervisor.get("verify_ssl", False),
            domain=domain,
        )

        manager = HyperVBackupManager(api)
        result = manager.restore_vm(
            import_path=import_path,
            vm_name=vm_name,
            restore_path=restore_path,
            vhd_destination_path=vhd_destination_path,
            smb_username=smb_username,
            smb_password=smb_password,
            smb_domain=smb_domain,
            network_mapping=network_mapping,
            start_after_restore=start_after,
            dry_run=True,
        )

        return result

    @app.get("/api/v1/hypervisors/{hypervisor_id}/catalog")
    async def get_hyperv_catalog(
        hypervisor_id: str,
        backup_path: str,
        repository_id: str | None = None,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Read the backup catalog from a Hyper-V backup repository.

        The catalog contains information about all VMs backed up to this path,
        their backup timestamps, sizes, and verification status.
        """
        from backer.hypervisors.hyperv import BackupCatalog, HyperVAPI

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        if hypervisor.get("hypervisor_type") not in ("hyperv", "hyperv-cluster"):
            raise HTTPException(status_code=400, detail="This endpoint is only for Hyper-V hypervisors")

        password = storage.get_hypervisor_password(hypervisor_id)
        if not password:
            raise HTTPException(status_code=400, detail="Hypervisor password not configured")

        domain = hypervisor.get("domain") or hypervisor.get("config", {}).get("domain")

        smb_username: str | None = None
        smb_password: str | None = None
        smb_domain: str | None = None

        if repository_id:
            repository = storage.get_repository(repository_id)
            if repository and repository.get("repo_type") == "smb":
                smb_username = repository.get("username", "")
                smb_password = storage.get_storage_password(repository_id)
                smb_domain = repository.get("domain", "")

        api = HyperVAPI(
            host=hypervisor["host"],
            username=hypervisor.get("username", "Administrator"),
            password=password,
            port=hypervisor.get("port", 5985),
            use_ssl=hypervisor.get("port", 5985) == 5986,
            verify_ssl=hypervisor.get("verify_ssl", False),
            domain=domain,
        )

        catalog = BackupCatalog(api)
        result = catalog.read_catalog(
            backup_path=backup_path,
            smb_username=smb_username,
            smb_password=smb_password,
            smb_domain=smb_domain,
        )

        if result is None:
            return {"version": None, "vms": {}, "message": "No catalog found at this path"}

        return result

    @app.post("/api/v1/hypervisors/{hypervisor_id}/catalog/rebuild")
    async def rebuild_hyperv_catalog(
        hypervisor_id: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Rebuild the backup catalog by scanning all backups in the repository.

        This is useful if the catalog was corrupted or if backups were
        manually added to the repository.
        """
        from backer.hypervisors.hyperv import BackupCatalog, HyperVAPI

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        if hypervisor.get("hypervisor_type") not in ("hyperv", "hyperv-cluster"):
            raise HTTPException(status_code=400, detail="This endpoint is only for Hyper-V hypervisors")

        body = await request.json()
        backup_path = body.get("backup_path")
        if not backup_path:
            raise HTTPException(status_code=400, detail="backup_path is required")

        repository_id = body.get("repository_id")

        password = storage.get_hypervisor_password(hypervisor_id)
        if not password:
            raise HTTPException(status_code=400, detail="Hypervisor password not configured")

        domain = hypervisor.get("domain") or hypervisor.get("config", {}).get("domain")

        smb_username: str | None = None
        smb_password: str | None = None
        smb_domain: str | None = None

        if repository_id:
            repository = storage.get_repository(repository_id)
            if repository and repository.get("repo_type") == "smb":
                smb_username = repository.get("username", "")
                smb_password = storage.get_storage_password(repository_id)
                smb_domain = repository.get("domain", "")

        api = HyperVAPI(
            host=hypervisor["host"],
            username=hypervisor.get("username", "Administrator"),
            password=password,
            port=hypervisor.get("port", 5985),
            use_ssl=hypervisor.get("port", 5985) == 5986,
            verify_ssl=hypervisor.get("verify_ssl", False),
            domain=domain,
        )

        catalog = BackupCatalog(api)
        result = catalog.rebuild_catalog(
            backup_path=backup_path,
            smb_username=smb_username,
            smb_password=smb_password,
            smb_domain=smb_domain,
        )

        return result

    @app.get("/api/v1/hypervisors/{hypervisor_id}/hyperv-backups")
    async def list_hyperv_backups(
        hypervisor_id: str,
        path: str | None = None,
        vm_name: str | None = None,
        repository_id: str | None = None,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List available Hyper-V VM backups in a directory.

        For SMB paths, provide repository_id to use the repository's SMB credentials
        for authentication when accessing the backup directory.

        Args:
            hypervisor_id: The Hyper-V hypervisor to query from
            path: UNC path to the backup directory
            vm_name: Optional filter by VM name
            repository_id: Optional repository ID for SMB credentials
        """
        from backer.hypervisors.hyperv import HyperVAPI, HyperVBackupManager

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        if hypervisor.get("hypervisor_type") not in ("hyperv", "hyperv-cluster"):
            raise HTTPException(status_code=400, detail="This endpoint is only for Hyper-V hypervisors")

        password = storage.get_hypervisor_password(hypervisor_id)
        if not password:
            raise HTTPException(status_code=400, detail="Hypervisor password not configured")

        if not path:
            raise HTTPException(status_code=400, detail="path query parameter is required")

        # Get domain from hypervisor data or config
        domain = hypervisor.get("domain") or hypervisor.get("config", {}).get("domain")

        # Get SMB credentials from repository if provided
        smb_username: str | None = None
        smb_password: str | None = None
        smb_domain: str | None = None

        if repository_id:
            repository = storage.get_repository(repository_id)
            if repository and repository.get("repo_type") == "smb":
                smb_username = repository.get("username", "")
                smb_password = storage.get_storage_password(repository_id)
                smb_domain = repository.get("domain", "")

        api = HyperVAPI(
            host=hypervisor["host"],
            username=hypervisor.get("username", "Administrator"),
            password=password,
            port=hypervisor.get("port", 5985),
            use_ssl=hypervisor.get("port", 5985) == 5986,
            verify_ssl=hypervisor.get("verify_ssl", False),
            domain=domain,
        )

        manager = HyperVBackupManager(api)
        return manager.list_backups(
            path,
            vm_name,
            smb_username=smb_username,
            smb_password=smb_password,
            smb_domain=smb_domain,
        )

    # ============ Logs API ============

    @app.get("/api/v1/logs")
    def list_log_files(
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List available log files."""
        log_dir = data_dir / "logs"
        if not log_dir.exists():
            return []

        log_files = []
        for log_file in sorted(log_dir.glob("*.log"), reverse=True):
            stat = log_file.stat()
            log_files.append(
                {
                    "name": log_file.name,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )

        return log_files

    @app.get("/api/v1/logs/{filename}")
    def get_log_content(
        filename: str,
        lines: int = 500,
        offset: int = 0,
        level: str | None = None,
        search: str | None = None,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Get content of a log file.

        Args:
            filename: Log file name
            lines: Number of lines to return (default 500)
            offset: Number of lines to skip from end (for pagination)
            level: Filter by log level (DEBUG, INFO, WARNING, ERROR)
            search: Search string to filter logs
        """
        import re

        # Validate filename to prevent path traversal
        if ".." in filename or "/" in filename or "\\" in filename:
            raise HTTPException(status_code=400, detail="Invalid filename")

        log_dir = data_dir / "logs"
        log_file = log_dir / filename

        if not log_file.exists() or not log_file.is_file():
            raise HTTPException(status_code=404, detail="Log file not found")

        # Read the file
        try:
            with open(log_file, encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to read log file: {e}")

        total_lines = len(all_lines)

        # Filter by log level if specified
        if level:
            level_upper = level.upper()
            all_lines = [line for line in all_lines if f" - {level_upper} - " in line]

        # Filter by search term if specified
        if search:
            search_lower = search.lower()
            all_lines = [line for line in all_lines if search_lower in line.lower()]

        filtered_total = len(all_lines)

        # Get the requested range (from end, newest first)
        if offset > 0:
            end_idx = len(all_lines) - offset
            start_idx = max(0, end_idx - lines)
            selected_lines = all_lines[start_idx:end_idx]
        else:
            # Get last N lines
            selected_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines

        # Reverse to show newest first
        selected_lines = list(reversed(selected_lines))

        # Parse log lines for structured output
        log_entries = []
        log_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - ([^-]+) - (\w+) - (.*)$")

        for line in selected_lines:
            line = line.rstrip("\n\r")
            match = log_pattern.match(line)
            if match:
                log_entries.append(
                    {
                        "timestamp": match.group(1),
                        "logger": match.group(2).strip(),
                        "level": match.group(3),
                        "message": match.group(4),
                        "raw": line,
                    }
                )
            else:
                # Non-matching line (continuation or different format)
                log_entries.append(
                    {
                        "timestamp": "",
                        "logger": "",
                        "level": "",
                        "message": line,
                        "raw": line,
                    }
                )

        return {
            "filename": filename,
            "total_lines": total_lines,
            "filtered_lines": filtered_total,
            "returned_lines": len(log_entries),
            "offset": offset,
            "entries": log_entries,
        }

    @app.get("/api/v1/logs/stream/{filename}")
    async def stream_log(
        filename: str,
        storage: Storage = Depends(get_storage),
    ):
        """Stream log file updates using Server-Sent Events."""
        import asyncio

        from fastapi.responses import StreamingResponse

        # Validate filename to prevent path traversal
        if ".." in filename or "/" in filename or "\\" in filename:
            raise HTTPException(status_code=400, detail="Invalid filename")

        log_dir = data_dir / "logs"
        log_file = log_dir / filename

        if not log_file.exists() or not log_file.is_file():
            raise HTTPException(status_code=404, detail="Log file not found")

        async def generate():
            """Generate SSE events for new log lines."""
            try:
                with open(log_file, encoding="utf-8", errors="replace") as f:
                    # Seek to end of file
                    f.seek(0, 2)

                    while True:
                        line = f.readline()
                        if line:
                            # Send as SSE event
                            yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"
                        else:
                            # No new data, wait a bit
                            await asyncio.sleep(0.5)
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    # ============ Proxy Repository Operations ============
    # These endpoints handle backup/restore/check operations for agents
    # using local directory storage via the proxy backend.

    def _verify_repo_access(
        repo_id: str,
        credentials: HTTPBasicCredentials | None,
        storage: Storage,
        authorization: str | None = None,
        *,
        operation: str,
        subfolder: str = "",
        capability: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Verify that an agent has access to a repository.

        Supports two authentication methods:
        1. Bearer token (JWT) via Authorization header
        2. HTTP Basic auth (client_id:client_secret)

        Returns: (repository_dict, password, capability_claims)
        Raises: HTTPException if access denied
        """
        # Verify repository exists and is local type
        repo = storage.get_repository(repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found")

        if repo.get("repo_type") != "local":
            raise HTTPException(status_code=400, detail="Repository is not local type")

        client_id: str | None = None
        # Check for bearer token first (preferred method)
        if authorization and authorization.startswith("Bearer "):
            token = authorization[7:]  # Remove "Bearer " prefix
            claims = verify_agent_token(token)
            if not claims:
                raise HTTPException(status_code=401, detail="Invalid or expired token")
            client_id = claims.get("sub")
        # Fall back to HTTP Basic auth
        elif credentials:
            client = storage.get_client(credentials.username)
            if not client:
                raise HTTPException(status_code=401, detail="Invalid client ID")

            secret_hash = storage.get_client_secret_hash(credentials.username)
            provided_hash = hashlib.sha256(credentials.password.encode()).hexdigest()

            if not secrets.compare_digest(secret_hash or "", provided_hash):
                raise HTTPException(status_code=401, detail="Invalid credentials")
            client_id = credentials.username
        else:
            raise HTTPException(status_code=401, detail="Auth required (Bearer or Basic)")

        claims = verify_proxy_capability(capability or "")
        if not claims:
            expired_claims = verify_expired_proxy_capability(capability or "")
            if expired_claims and _pending_proxy_command_authorizes(storage, client_id, expired_claims, operation):
                claims = expired_claims
        if not claims or any(
            (
                claims.get("repo") != repo_id,
                claims.get("sub") != client_id,
                claims.get("operation") != operation
                and not (operation == "check" and claims.get("operation") in {"backup", "restore"}),
                bool(subfolder) and claims.get("subfolder") != subfolder,
            )
        ):
            raise HTTPException(status_code=403, detail="Missing, expired, or invalid proxy capability")

        # Get repository password (secure)
        password = storage.get_repository_password(repo_id) or ""

        return repo, password, claims

    @app.get("/api/repo/{repo_id}/check")
    async def proxy_repo_check(
        repo_id: str,
        request: Request,
        credentials: HTTPBasicCredentials | None = Depends(security),
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Check if a repository is available (health check for proxy backend)."""
        auth_header = request.headers.get("authorization")
        repo, _, _claims = _verify_repo_access(
            repo_id,
            credentials,
            storage,
            auth_header,
            operation="check",
            capability=request.headers.get("X-Backer-Capability"),
        )

        # Check if local path exists and is readable
        # For local repos, the path is stored in the 'share' field
        local_path = repo.get("share")
        if not local_path:
            return {"available": False, "error": "No local path configured"}

        path = Path(local_path)
        if not path.exists():
            return {"available": False, "error": f"Path does not exist: {local_path}"}

        if not path.is_dir():
            return {"available": False, "error": f"Path is not a directory: {local_path}"}

        if not os.access(path, os.R_OK):
            return {"available": False, "error": f"Path is not readable: {local_path}"}

        return {"available": True, "message": "OK"}

    @app.post("/api/repo/{repo_id}/init")
    async def proxy_repo_init(
        repo_id: str,
        request: Request,
        credentials: HTTPBasicCredentials | None = Depends(security),
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Initialize a backup repository on the server."""
        auth_header = request.headers.get("authorization")
        repo, repo_password, _claims = _verify_repo_access(
            repo_id,
            credentials,
            storage,
            auth_header,
            operation="init",
            capability=request.headers.get("X-Backer-Capability"),
        )

        # Parse request body (password may be sent for verification)
        try:
            await request.json()  # Consume body but use repository password
        except Exception:
            pass

        local_path = repo.get("share")
        if not local_path:
            return {"success": False, "error": "No local path configured"}

        path = Path(local_path)

        # Ensure directory exists and is writable
        try:
            path.mkdir(parents=True, exist_ok=True)
            if not os.access(path, os.W_OK):
                return {"success": False, "error": f"Path is not writable: {local_path}"}
        except Exception as e:
            return {"success": False, "error": f"Failed to create/access directory: {e}"}

        try:
            if repo.get("repo_type") == "local":
                kopia = ServerKopia(str(path), _repository_password_or_error(repo_password))
                return {"success": kopia.ensure_repo(), "integrity": "repository validation"}
            # Import here to avoid circular imports
            from backer.backends.base import BackupDestination
            from backer.backends.registry import get_backend

            backend = get_backend(
                "kopia",
                {
                    "repository_password": _repository_password_or_error(repo_password),
                },
            )

            dest = BackupDestination(path=str(path))
            result = backend.init_repo(dest)

            return {
                "success": result.success,
                "output": result.output,
                "error": result.errors[0] if result.errors else None,
            }
        except Exception as e:
            logger.error(f"Failed to init repository {repo_id}: {e}")
            return {"success": False, "error": str(e)}

    @app.get("/api/repo/{repo_id}/snapshots")
    async def proxy_repo_snapshots(
        repo_id: str,
        request: Request,
        job: str | None = None,
        credentials: HTTPBasicCredentials | None = Depends(security),
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """List snapshots in the repository.

        Query parameters:
            job: Optional job name to filter snapshots (LOCAL repos only)
        """
        auth_header = request.headers.get("authorization")
        repo, repo_password, _claims = _verify_repo_access(
            repo_id,
            credentials,
            storage,
            auth_header,
            operation="list",
            capability=request.headers.get("X-Backer-Capability"),
        )

        try:
            local_path = repo.get("share")
            repo_type = repo.get("repo_type", "").lower()

            # For LOCAL repos, use ServerKopia to list snapshots
            if repo_type == "local":
                password = _repository_password_or_error(repo_password)
                kopia = ServerKopia(local_path, password)

                if not kopia.ensure_repo():
                    return {"success": False, "error": "Kopia repository not initialized", "snapshots": []}

                snapshots = kopia.snapshot_list(job_name=job)
                return {
                    "success": True,
                    "snapshots": snapshots,
                    "count": len(snapshots),
                }

            # For SMB/NFS repos, use the backend's list_snapshots
            from backer.backends.base import BackupDestination
            from backer.backends.registry import get_backend

            backend = get_backend(
                "kopia",
                {
                    "repository_password": _repository_password_or_error(repo_password),
                },
            )

            dest = BackupDestination(path=str(local_path))
            snapshots = backend.list_snapshots(dest)

            return {"snapshots": snapshots}
        except Exception as e:
            logger.error(f"Failed to list snapshots for {repo_id}: {e}")
            return {"snapshots": [], "error": str(e)}

    @app.post("/api/repo/{repo_id}/prune")
    async def proxy_repo_prune(
        repo_id: str,
        request: Request,
        credentials: HTTPBasicCredentials | None = Depends(security),
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Prune the repository to remove unused data."""
        auth_header = request.headers.get("authorization")
        repo, repo_password, capability_claims = _verify_repo_access(
            repo_id,
            credentials,
            storage,
            auth_header,
            operation="prune",
            capability=request.headers.get("X-Backer-Capability"),
        )

        try:
            body = await request.json()
        except Exception:
            body = {}

        dry_run = bool(body.get("dry_run", False))
        source_path = body.get("source_path")
        if source_path is not None and not isinstance(source_path, str):
            return {"success": False, "error": "source_path must be a string"}
        if isinstance(source_path, str) and source_path.startswith("-"):
            # source_path is passed to kopia as a positional argument. A value
            # like "--all" would turn a source-scoped expiry into a
            # repository-wide one and delete every job's snapshots.
            return {"success": False, "error": "source_path must not start with '-'"}

        try:
            policy = _validate_retention_policy(body)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        # Fail closed: never delete under a policy nobody explicitly configured.
        if not policy:
            return {
                "success": False,
                "error": "Refusing to prune: no retention policy was supplied",
            }

        try:
            local_path = repo.get("share")
            # _verify_repo_access already rejects any repo_type other than
            # "local" with a 400, so this is the only branch that ever runs.
            kopia = ServerKopia(str(local_path), _repository_password_or_error(repo_password))
            if not kopia.ensure_repo():
                return {"success": False, "error": "Kopia repository not initialized"}

            # source_path is the caller-supplied (agent-side) path and can
            # never match what kopia recorded: the server snapshots
            # {share}/Agents/{job}/contents under its own OS identity and only
            # keeps the agent path as a "source:" tag (see proxy_repo_backup).
            # Trusting it as a kopia positional target either creates a
            # phantom policy that prunes nothing, or - for forms like
            # "user@host" - silently widens to every source in the
            # repository. Derive the target instead from the job the
            # capability was actually issued for, resolved against kopia's
            # own snapshot list, and refuse if nothing matches.
            job_name = capability_claims.get("job") if capability_claims else None
            matches = kopia.snapshot_list(job_name=job_name) if job_name else []
            target = matches[0].get("source") if matches else None
            if not target:
                return {
                    "success": False,
                    "error": (
                        f"Could not resolve a kopia snapshot source for job "
                        f"'{job_name}' - refusing to prune rather than risk "
                        "applying the policy repository-wide."
                    ),
                }

            # Only write the policy on a real run. "snapshot expire" has no
            # ad-hoc keep flags of its own - it only ever evaluates the
            # policy already persisted - so a dry run must not persist one:
            # doing so previously armed deletion at the next ordinary
            # snapshot, with nothing connecting it back to the preview.
            if not dry_run:
                policy_args = ["policy", "set", target]
                for key, flag in _RETENTION_KOPIA_FLAGS.items():
                    if key in policy:
                        policy_args.extend([flag, str(policy[key])])
                policy_result = kopia.maintenance(policy_args)
                if not policy_result.get("success"):
                    return policy_result

            expire_args = ["snapshot", "expire", target]
            if not dry_run:
                expire_args.append("--delete")
            result = kopia.maintenance(expire_args)
            if dry_run:
                result["note"] = (
                    "Dry run: this reports against the retention policy currently "
                    "persisted in kopia, not the policy proposed in this request - "
                    "kopia cannot preview a policy without saving it first."
                )
            return result
        except Exception as e:
            logger.error(f"Failed to prune repository {repo_id}: {e}")
            return {"success": False, "error": str(e)}

    @app.post("/api/repo/{repo_id}/check-integrity")
    async def proxy_repo_check_integrity(
        repo_id: str,
        request: Request,
        credentials: HTTPBasicCredentials | None = Depends(security),
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Check repository integrity."""
        auth_header = request.headers.get("authorization")
        repo, repo_password, _claims = _verify_repo_access(
            repo_id,
            credentials,
            storage,
            auth_header,
            operation="check",
            capability=request.headers.get("X-Backer-Capability"),
        )

        try:
            local_path = repo.get("share")
            # _verify_repo_access already rejects any repo_type other than
            # "local" with a 400, so this is the only branch that ever runs.
            kopia = ServerKopia(str(local_path), _repository_password_or_error(repo_password))
            if not kopia.ensure_repo():
                return {
                    "success": False,
                    "error": "Kopia repository not initialized",
                    "integrity": "snapshot verification",
                }
            # "repository status" only reports config/connection state - it
            # validates nothing about the stored data. "snapshot verify" is
            # the actual integrity check; --verify-files-percent=0 verifies
            # metadata/structure without reading file content, matching
            # KopiaBackend.check()'s default.
            result = kopia.maintenance(["snapshot", "verify", "--verify-files-percent=0"])
            return {**result, "integrity": "snapshot verification"}
        except Exception as e:
            logger.error(f"Failed to check repository {repo_id}: {e}")
            return {"success": False, "error": str(e)}

    @app.post("/api/repo/{repo_id}/backup")
    async def proxy_repo_backup(
        repo_id: str,
        request: Request,
        credentials: HTTPBasicCredentials | None = Depends(security),
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Receive backup data from agent and store in proper directory structure.

        Flow:
        1. Receive tar.gz stream from agent
        2. Extract to {local_path}/Agents/{job_name}/contents/ (persistent storage)
        3. Create kopia snapshot with job tags (for versioning/point-in-time restore)

        Files remain accessible in the filesystem structure while kopia provides versioning.
        """
        import tempfile

        auth_header = request.headers.get("authorization")
        subfolder = request.headers.get("X-Backup-Subfolder", "").strip("/\\").replace("\\", "/")
        repo, repo_password, _claims = _verify_repo_access(
            repo_id,
            credentials,
            storage,
            auth_header,
            operation="backup",
            subfolder=subfolder,
            capability=request.headers.get("X-Backer-Capability"),
        )

        local_path = repo.get("share")
        if not local_path:
            return {"success": False, "error": "No local path configured"}

        # Get destination subfolder from headers (e.g., "Agents/testjob")
        subfolder = request.headers.get("X-Backup-Subfolder", "")
        source_path = request.headers.get("X-Source-Path", "unknown")

        # Extract job name from subfolder (e.g., "Agents/testjob" -> "testjob")
        # Handle both forward slashes and backslashes for Windows compatibility
        job_name = "unknown"
        if subfolder:
            subfolder = subfolder.strip("/\\")
            # Normalize to forward slashes for consistent parsing
            subfolder = subfolder.replace("\\", "/")
            parts = subfolder.split("/")
            if len(parts) >= 2 and parts[0] == "Agents":
                job_name = parts[1]
            elif parts:
                job_name = parts[-1]

        logger.info(f"[PROXY BACKUP] Receiving backup for job '{job_name}' from {source_path}")

        # Build the proper backup directory structure: {local_path}/Agents/{job_name}/contents/
        # This matches the structure used by SMB/NFS repositories for consistency
        local_base = Path(local_path)
        backup_dir = local_base / "Agents" / job_name / "contents"
        tmp_path = None
        staging_dir: Path | None = None

        try:
            # Stream body to temp file
            with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
                tmp_path = tmp.name
                bytes_received = 0
                async for chunk in request.stream():
                    tmp.write(chunk)
                    bytes_received += len(chunk)

            logger.info(f"[PROXY BACKUP] Received {bytes_received / 1024 / 1024:.1f}MB")

            # Validate and extract into a sibling staging directory.  A failed
            # upload must never leave a partly-updated live backup behind.
            backup_dir.parent.mkdir(parents=True, exist_ok=True)
            staging_dir = Path(tempfile.mkdtemp(prefix=".backer-stage-", dir=backup_dir.parent))
            members = safe_tar_extract(tmp_path, staging_dir)

            # The lock survives workers and process restarts, unlike an asyncio
            # lock. It lives outside contents and rollback-directory globs.
            transaction_lock = (
                local_base / ".backer-locks" / (f"proxy-{hashlib.sha256(job_name.encode()).hexdigest()}.lock")
            )
            # One repository/job has one live contents directory. Keep the old
            # tree until Kopia accepts the replacement, then discard it.
            with file_lock(transaction_lock):
                previous_dir = backup_dir.parent / f".backer-previous-{uuid4().hex}"
                failed_dir = backup_dir.parent / f".backer-failed-{uuid4().hex}"
                replaced = False
                previous_moved = False
                try:
                    if backup_dir.exists():
                        os.replace(backup_dir, previous_dir)
                        previous_moved = True
                    os.replace(staging_dir, backup_dir)
                    staging_dir = None
                    replaced = True

                    logger.info(f"[PROXY BACKUP] Extracted {len(members)} files to {backup_dir}")
                    password = _repository_password_or_error(repo_password)
                    kopia = ServerKopia(local_path, password)
                    if not kopia.ensure_repo():
                        raise RuntimeError("Failed to initialize kopia repository")
                    result = kopia.snapshot_create(
                        source_dir=backup_dir,
                        job_name=job_name,
                        source_path=source_path,
                    )
                    if not result.get("success"):
                        raise RuntimeError(result.get("error", "Snapshot creation failed"))
                except Exception as snapshot_error:
                    rollback_error = None
                    if replaced:
                        try:
                            os.replace(backup_dir, failed_dir)
                        except Exception as move_error:
                            rollback_error = RuntimeError(
                                "CRITICAL: rollback could not preserve rejected contents; "
                                f"previous contents remain at {previous_dir}: {move_error}"
                            )
                        else:
                            if previous_moved:
                                try:
                                    os.replace(previous_dir, backup_dir)
                                except Exception as restore_error:
                                    rollback_error = RuntimeError(
                                        "CRITICAL: rollback could not restore previous contents; "
                                        f"previous contents retained at {previous_dir}: {restore_error}"
                                    )
                            try:
                                shutil.rmtree(failed_dir)
                            except Exception as cleanup_error:
                                logger.warning(
                                    "[PROXY BACKUP] Failed to clean rejected contents %s: %s",
                                    failed_dir,
                                    cleanup_error,
                                )
                    elif previous_moved:
                        try:
                            os.replace(previous_dir, backup_dir)
                        except Exception as restore_error:
                            rollback_error = RuntimeError(
                                "CRITICAL: rollback could not restore previous contents; "
                                f"previous contents retained at {previous_dir}: {restore_error}"
                            )
                    if rollback_error:
                        logger.critical("[PROXY BACKUP] %s", rollback_error)
                        raise rollback_error from snapshot_error
                    raise
                else:
                    if previous_dir.exists():
                        try:
                            shutil.rmtree(previous_dir)
                        except Exception as cleanup_error:
                            logger.warning(
                                "[PROXY BACKUP] Snapshot committed but failed to clean previous contents %s: %s",
                                previous_dir,
                                cleanup_error,
                            )

            # Clean up tar file
            try:
                Path(tmp_path).unlink()
            except Exception as cleanup_error:
                logger.warning(
                    "[PROXY BACKUP] Snapshot committed but failed to clean upload archive: %s",
                    cleanup_error,
                )
            tmp_path = None

            snapshot_id = result.get("snapshot_id", "unknown")
            logger.info(f"[PROXY BACKUP] Created kopia snapshot: {snapshot_id}")

            # Write repository metadata for disaster recovery
            # Metadata goes at job folder level: {local_path}/Agents/{job_name}/.backer/
            try:
                from backer.core.repo_metadata import RepositoryMetadata

                job_folder = local_base / "Agents" / job_name
                repo_meta = RepositoryMetadata(job_folder, repo_type="local")

                # Initialize metadata if not already done
                if not repo_meta.is_initialized():
                    repo_meta.initialize()
                    logger.info(f"[PROXY BACKUP] Initialized metadata at {job_folder}/.backer/")

                # Extract client_id from auth for agent metadata
                client_id = None
                client = None
                if auth_header and auth_header.startswith("Bearer "):
                    token = auth_header[7:]
                    claims = verify_agent_token(token)
                    if claims:
                        client_id = claims.get("sub")
                elif credentials:
                    client_id = credentials.username

                # Save agent metadata if we have client info
                if client_id:
                    client = storage.get_client(client_id)
                    if client:
                        agent_data = {
                            "hostname": client.hostname,
                            "os_info": client.os_info,
                            "version": client.version,
                            "ip_address": client.ip_address,
                            "name": client.name,
                        }
                        repo_meta.save_agent(client_id, agent_data)
                        logger.debug(f"[PROXY BACKUP] Saved agent metadata for {client.hostname}")

                # Save job configuration
                job_config = {
                    "source_path": source_path,
                    "repo_id": repo_id,
                    "client_id": client_id,
                }
                repo_meta.save_job(job_name, job_config)

                # Save job run record
                run_data = {
                    "status": "completed",
                    "started_at": datetime.now().isoformat(),
                    "finished_at": datetime.now().isoformat(),
                    "snapshot_id": snapshot_id,
                    "bytes_transferred": bytes_received,
                    "files_transferred": len(members),
                    "client_id": client_id,
                    "hostname": client.hostname if client else None,
                }
                run_id = f"{job_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                repo_meta.save_job_run(job_name, run_id, run_data)

                logger.info(f"[PROXY BACKUP] Saved metadata for job '{job_name}'")
            except Exception as meta_err:
                # Log but don't fail the backup if metadata writing fails
                logger.warning(f"[PROXY BACKUP] Failed to write metadata: {meta_err}")

            return {
                "success": True,
                "message": f"Backup stored: {len(members)} files, {bytes_received / 1024 / 1024:.1f}MB",
                "files": len(members),
                "bytes": bytes_received,
                "snapshot_id": snapshot_id,
                "backup_path": str(backup_dir),
            }

        except Exception as e:
            logger.error(f"[PROXY BACKUP] Failed: {e}")
            return {"success": False, "error": str(e)}

        finally:
            if staging_dir:
                try:
                    shutil.rmtree(staging_dir)
                except Exception as cleanup_error:
                    logger.warning(
                        "[PROXY BACKUP] Failed to clean staging directory %s: %s",
                        staging_dir,
                        cleanup_error,
                    )
            # Clean up temp tar file if it still exists
            if tmp_path:
                try:
                    Path(tmp_path).unlink()
                except Exception as cleanup_error:
                    logger.warning("[PROXY BACKUP] Failed to clean upload archive %s: %s", tmp_path, cleanup_error)

    @app.get("/api/repo/{repo_id}/restore")
    async def proxy_repo_restore(
        repo_id: str,
        request: Request,
        background_tasks: BackgroundTasks,
        snapshot: str | None = None,
        credentials: HTTPBasicCredentials | None = Depends(security),
        storage: Storage = Depends(get_storage),
    ):
        """Restore from kopia snapshot and stream as tar.gz archive.

        Query parameters:
            snapshot: Specific snapshot ID to restore (optional, defaults to latest)

        Headers:
            X-Restore-Subfolder: Job subfolder (e.g., "Agents/testjob")
        """
        import shutil
        import tarfile
        import tempfile

        auth_header = request.headers.get("authorization")
        subfolder = request.headers.get("X-Restore-Subfolder", "").strip("/\\").replace("\\", "/")
        repo, repo_password, _claims = _verify_repo_access(
            repo_id,
            credentials,
            storage,
            auth_header,
            operation="restore",
            subfolder=subfolder,
            capability=request.headers.get("X-Backer-Capability"),
        )

        local_path = repo.get("share")
        if not local_path:
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "No local path configured"},
            )

        # Get restore subfolder from headers (e.g., "Agents/testjob")
        subfolder = request.headers.get("X-Restore-Subfolder", "")

        # Extract job name from subfolder
        # Handle both forward slashes and backslashes for Windows compatibility
        job_name = "unknown"
        if subfolder:
            subfolder = subfolder.strip("/\\")
            # Normalize to forward slashes for consistent parsing
            subfolder = subfolder.replace("\\", "/")
            parts = subfolder.split("/")
            if len(parts) >= 2 and parts[0] == "Agents":
                job_name = parts[1]
            elif parts:
                job_name = parts[-1]

        logger.info(f"[PROXY RESTORE] Restoring job '{job_name}', snapshot={snapshot or 'latest'}")

        # Get repository password
        password = _repository_password_or_error(repo_password)

        # Initialize kopia
        kopia = ServerKopia(local_path, password)

        if not kopia.ensure_repo():
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "Kopia repository not initialized"},
            )

        # Determine snapshot to restore
        snapshot_id = snapshot
        if not snapshot_id or snapshot_id == "latest":
            # Find the latest snapshot for this job
            snapshot_id = kopia.find_latest_snapshot(job_name)
            if not snapshot_id:
                return JSONResponse(
                    status_code=404,
                    content={"success": False, "error": f"No snapshots found for job '{job_name}'"},
                )
            logger.info(f"[PROXY RESTORE] Using latest snapshot: {snapshot_id}")

        # Create staging directory for restore
        staging_dir = None
        tmp_path = None

        try:
            staging_dir = tempfile.mkdtemp(prefix=f"backer-restore-{job_name}-")
            staging_path = Path(staging_dir)

            # Restore snapshot to staging
            result = kopia.snapshot_restore(snapshot_id, staging_path)
            if not result.get("success"):
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": result.get("error", "Restore failed")},
                )

            logger.info(f"[PROXY RESTORE] Restored snapshot to staging: {staging_path}")

            # Create tar archive from staging
            with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
                tmp_path = tmp.name

            # Create tar archive with all files
            # Use .as_posix() for arcname to ensure portable paths
            with tarfile.open(tmp_path, "w:gz") as tar:
                for item in staging_path.rglob("*"):
                    if item.is_file():
                        arcname = item.relative_to(staging_path).as_posix()
                        tar.add(str(item), arcname=arcname)

            # Get archive size
            archive_size = Path(tmp_path).stat().st_size
            logger.info(f"[PROXY RESTORE] Archive created: {archive_size / 1024 / 1024:.1f}MB")

            # Track restore operation in metadata for audit trail
            try:
                from backer.core.repo_metadata import RepositoryMetadata

                local_base = Path(local_path)
                job_folder = local_base / "Agents" / job_name
                repo_meta = RepositoryMetadata(job_folder, repo_type="local")

                if repo_meta.is_initialized():
                    # Extract client_id from auth for tracking
                    restore_client_id = None
                    if auth_header and auth_header.startswith("Bearer "):
                        token = auth_header[7:]
                        claims = verify_agent_token(token)
                        if claims:
                            restore_client_id = claims.get("sub")
                    elif credentials:
                        restore_client_id = credentials.username

                    # Get client info for hostname
                    restore_hostname = None
                    if restore_client_id:
                        restore_client = storage.get_client(restore_client_id)
                        if restore_client:
                            restore_hostname = restore_client.hostname

                    # Save restore operation record
                    restore_run_data = {
                        "operation_type": "restore",
                        "status": "completed",
                        "started_at": datetime.now().isoformat(),
                        "finished_at": datetime.now().isoformat(),
                        "snapshot_id": snapshot_id,
                        "bytes_transferred": archive_size,
                        "client_id": restore_client_id,
                        "hostname": restore_hostname,
                    }
                    restore_run_id = f"restore_{job_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                    repo_meta.save_job_run(job_name, restore_run_id, restore_run_data)
                    logger.info(f"[PROXY RESTORE] Saved restore metadata for job '{job_name}'")
            except Exception as meta_err:
                logger.warning(f"[PROXY RESTORE] Failed to write restore metadata: {meta_err}")

            # Clean up staging directory before streaming response
            shutil.rmtree(staging_dir)
            staging_dir = None

            # Stream file to client
            from fastapi.responses import FileResponse

            # Schedule cleanup of temp file after response is sent
            # FileResponse does NOT auto-delete temp files, so we use BackgroundTasks
            def cleanup_temp_file(path: str) -> None:
                try:
                    Path(path).unlink()
                    logger.debug(f"[PROXY RESTORE] Cleaned up temp file: {path}")
                except Exception as e:
                    logger.warning(f"[PROXY RESTORE] Failed to clean temp file {path}: {e}")

            background_tasks.add_task(cleanup_temp_file, tmp_path)

            response = FileResponse(
                path=tmp_path,
                media_type="application/gzip",
                filename=f"backup-{repo_id}-{snapshot_id[:8]}.tar.gz",
                headers={"Content-Disposition": f"attachment; filename=backup-{repo_id}.tar.gz"},
            )
            return response

        except Exception as e:
            logger.error(f"[PROXY RESTORE] Failed: {e}")
            # Clean up temp file on error (not handled by background task in error case)
            if tmp_path and Path(tmp_path).exists():
                try:
                    Path(tmp_path).unlink()
                except Exception:
                    pass
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": str(e)},
            )

        finally:
            # Clean up staging directory if it still exists
            if staging_dir and Path(staging_dir).exists():
                try:
                    shutil.rmtree(staging_dir)
                except Exception as cleanup_err:
                    logger.warning(f"[PROXY RESTORE] Failed to clean staging: {cleanup_err}")

    # ============ Web UI ============

    # Store storage in app state for web routes
    app.state.storage = _storage

    # Mount static files
    static_dir = Path(__file__).parent / "web" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Add web authentication middleware
    from backer.server.web.auth import AuthMiddleware

    app.add_middleware(AuthMiddleware)

    # Include web UI routes
    app.include_router(web_router)

    return app
