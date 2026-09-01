"""Repository metadata management for backup discovery and tracking.

This module provides functionality to store and retrieve backup metadata
directly in the repository, enabling:
- Discovery of existing backups when connecting a new server
- Cross-server backup history
- Disaster recovery of backup records

Metadata Structure in Repository:
    .backer/
    ├── metadata.json           # Repository info
    ├── agents/
    │   └── {agent_id}.json     # Per-agent metadata
    ├── jobs/
    │   └── {job_name}/
    │       ├── config.json     # Job configuration
    │       └── runs/
    │           └── {run_id}.json  # Individual run records
    └── snapshots/
        └── {snapshot_id}.json  # Snapshot metadata
"""

import json
import logging
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

# Cross-platform file locking
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

logger = logging.getLogger(__name__)

# Backer metadata directory name
BACKER_METADATA_DIR = ".backer"
METADATA_VERSION = "1.0"


@contextmanager
def file_lock(file_path: Path, mode: str = "r+"):
    """Cross-platform file locking context manager.

    Acquires an exclusive lock on a file before yielding the file handle.
    Automatically releases the lock when exiting the context.

    Args:
        file_path: Path to the file to lock
        mode: File open mode (default: 'r+' for read-write)

    Yields:
        Open file handle with lock acquired

    Note:
        - On Windows: Uses msvcrt.locking() for exclusive locks
        - On Unix: Uses fcntl.flock() with LOCK_EX
        - Lock is automatically released when file is closed
    """
    # Ensure parent directory exists
    file_path.parent.mkdir(parents=True, exist_ok=True)

    # Create file if it doesn't exist (for 'r+' mode)
    if not file_path.exists():
        file_path.touch()

    f = open(file_path, mode, encoding="utf-8")
    lock_acquired = False
    try:
        if sys.platform == "win32":
            # Windows: Lock bytes from start of file
            # msvcrt.locking() locks a range of bytes, use a reasonable size
            # for our small JSON metadata files (64KB should be plenty)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 65536)
                lock_acquired = True
            except OSError:
                # If non-blocking fails, try blocking lock
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 65536)
                lock_acquired = True
        else:
            # Unix: Exclusive lock on entire file
            fcntl.flock(f, fcntl.LOCK_EX)
            lock_acquired = True

        yield f

    finally:
        if sys.platform == "win32" and lock_acquired:
            try:
                f.seek(0)  # Ensure we're at start for unlock
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 65536)
            except Exception:
                pass  # May already be unlocked or file closed
        # Unix: flock automatically releases on close
        f.close()


def normalize_path_for_platform(path: str, repo_type: str = "local") -> str:
    """Normalize a path for the current platform.

    On Windows:
    - Converts forward-slash UNC paths (//server/share) to backslash format
    - Converts NFS-style paths (server:/share) to UNC format for Windows NFS client

    Args:
        path: The path to normalize
        repo_type: Type of repository (local, smb, nfs)

    Returns:
        Normalized path for the current platform
    """
    if sys.platform == 'win32':
        # Convert forward slash UNC paths to backslash format
        if path.startswith('//'):
            return path.replace('/', '\\')

        # Convert NFS-style paths (server:/share) to Windows UNC format
        # Windows NFS client uses UNC paths like \\server\share
        if repo_type == "nfs" and ':' in path and not path[1:2] == ':':
            # Looks like server:/path format (not C:\path)
            parts = path.split(':', 1)
            if len(parts) == 2 and parts[0] and parts[1]:
                server = parts[0]
                share_path = parts[1].replace('/', '\\').lstrip('\\')
                return f"\\\\{server}\\{share_path}"

    return path


class RepositoryMetadataError(Exception):
    """Error accessing or writing repository metadata."""
    pass


class RepositoryMetadata:
    """Manages metadata stored within a backup repository.

    This class handles reading and writing metadata files to the repository
    itself, enabling backup discovery and cross-server synchronization.
    """

    def __init__(self, repo_path: str | Path, repo_type: str = "local"):
        """Initialize repository metadata manager.

        Args:
            repo_path: Path to the repository (local path, SMB share, etc.)
            repo_type: Type of repository (local, smb, nfs, s3)
        """
        self.repo_type = repo_type
        # Normalize path for the current platform (handles Windows UNC/NFS paths)
        if isinstance(repo_path, str):
            repo_path = normalize_path_for_platform(repo_path, repo_type)
        self.repo_path = Path(repo_path) if isinstance(repo_path, str) else repo_path
        self.metadata_dir = self.repo_path / BACKER_METADATA_DIR
        logger.debug(f"RepositoryMetadata: path={self.repo_path}, type={repo_type}")

    def _ensure_dirs(self) -> None:
        """Ensure metadata directory structure exists."""
        try:
            self.metadata_dir.mkdir(parents=True, exist_ok=True)
            (self.metadata_dir / "agents").mkdir(exist_ok=True)
            (self.metadata_dir / "jobs").mkdir(exist_ok=True)
            (self.metadata_dir / "snapshots").mkdir(exist_ok=True)
        except OSError as e:
            raise RepositoryMetadataError(f"Failed to create metadata directories: {e}")

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        """Read a JSON file from the repository.

        Falls back to sudo cat if normal read fails with permission error.
        This helps with NFS mounts where file ownership doesn't match.
        """
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
            return None
        except PermissionError:
            # Try sudo fallback for NFS permission issues
            return self._read_json_sudo(path)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read {path}: {e}")
            return None

    def _read_json_sudo(self, path: Path) -> dict[str, Any] | None:
        """Read a JSON file using sudo (fallback for NFS permission issues)."""
        import subprocess

        try:
            result = subprocess.run(
                ["sudo", "-n", "cat", str(path)],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
            logger.warning(f"sudo cat failed for {path}: {result.stderr}")
            return None
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read {path} with sudo: {e}")
            return None

    def _list_dirs_sudo(self, path: Path) -> list[str]:
        """List directories using sudo (fallback for NFS permission issues)."""
        import subprocess

        try:
            result = subprocess.run(
                ["sudo", "-n", "ls", "-1", str(path)],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                return [d for d in result.stdout.strip().split('\n') if d]
            return []
        except (subprocess.TimeoutExpired, OSError):
            return []

    def _list_json_files_sudo(self, path: Path) -> list[dict[str, Any]]:
        """List and read JSON files in a directory using sudo."""
        import subprocess

        results = []
        try:
            # List files
            ls_result = subprocess.run(
                ["sudo", "-n", "ls", "-1", str(path)],
                capture_output=True,
                text=True,
                timeout=10
            )
            if ls_result.returncode != 0:
                return []

            for filename in ls_result.stdout.strip().split('\n'):
                if filename.endswith('.json'):
                    data = self._read_json_sudo(path / filename)
                    if data:
                        results.append(data)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return results

    def _write_json(self, path: Path, data: dict[str, Any]) -> bool:
        """Write a JSON file to the repository atomically.

        Writes to a uniquely-named temp file in the same directory and
        atomically renames it onto the target with os.replace(). This is
        atomic on both POSIX and Windows, and (unlike advisory file locks,
        which are unreliable over CIFS/NFS) the rename is evaluated by the
        server rather than the client. A reader can never observe a
        truncated/partial file, and an interrupted write leaves the target
        untouched.
        """
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, default=str)
                # On Windows, concurrent os.replace() calls onto the same
                # destination can transiently race with WinError 5 (Access
                # Denied) even though the operation is atomic - retry a
                # few times before giving up.
                # ponytail: fixed retry count, revisit with backoff/jitter
                # if this shows up under heavier contention than tests do.
                for attempt in range(5):
                    try:
                        os.replace(tmp_path, path)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(0.05)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            return True
        except OSError as e:
            logger.error(f"Failed to write {path}: {e}")
            return False

    def is_initialized(self) -> bool:
        """Check if repository has Backer metadata."""
        metadata_file = self.metadata_dir / "metadata.json"
        exists = metadata_file.exists()
        logger.debug(f"Checking metadata at {metadata_file}: exists={exists}")
        if not exists:
            # Log additional debug info to help diagnose path issues
            logger.debug(f"  repo_path exists: {self.repo_path.exists() if self.repo_path else False}")
            logger.debug(f"  metadata_dir exists: {self.metadata_dir.exists() if self.metadata_dir else False}")
        return exists

    def initialize(self, server_id: str | None = None) -> dict[str, Any]:
        """Initialize metadata in the repository.

        Creates the metadata directory structure and writes initial metadata.

        Args:
            server_id: Optional server identifier

        Returns:
            The created metadata dict
        """
        self._ensure_dirs()

        metadata = {
            "version": METADATA_VERSION,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "repo_type": self.repo_type,
            "server_id": server_id,
            "backer_version": self._get_backer_version(),
        }

        self._write_json(self.metadata_dir / "metadata.json", metadata)
        logger.info(f"Initialized repository metadata at {self.metadata_dir}")
        return metadata

    def _get_backer_version(self) -> str:
        """Get the current Backer version."""
        try:
            from backer import __version__
            return __version__
        except ImportError:
            return "unknown"

    def get_metadata(self) -> dict[str, Any] | None:
        """Get repository metadata."""
        return self._read_json(self.metadata_dir / "metadata.json")

    def update_metadata(self, **kwargs: Any) -> bool:
        """Update repository metadata fields."""
        metadata = self.get_metadata() or {}
        metadata.update(kwargs)
        metadata["updated_at"] = datetime.now().isoformat()
        return self._write_json(self.metadata_dir / "metadata.json", metadata)

    # Agent metadata methods
    def save_agent(self, agent_id: str, agent_data: dict[str, Any]) -> bool:
        """Save agent metadata to repository (thread-safe).

        Args:
            agent_id: Unique agent identifier
            agent_data: Agent information (hostname, os_info, etc.)
        """
        self._ensure_dirs()

        agent_path = self.metadata_dir / "agents" / f"{agent_id}.json"

        try:
            existing = self._read_json(agent_path) or {}
            existing.update(agent_data)
            now = datetime.now().isoformat()
            existing["agent_id"] = agent_id
            existing["schema_version"] = "2"
            existing["backer_version"] = self._get_backer_version()
            existing.setdefault("modes", ["server"])
            existing["updated_at"] = now
            existing.setdefault("first_seen", now)
            return self._write_json(agent_path, existing)
        except Exception as e:
            logger.error(f"Failed to save agent {agent_id}: {e}")
            return False

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        """Get agent metadata from repository."""
        return self._read_json(self.metadata_dir / "agents" / f"{agent_id}.json")

    def list_agents(self) -> list[dict[str, Any]]:
        """List all agents that have backed up to this repository."""
        agents_dir = self.metadata_dir / "agents"
        if not agents_dir.exists():
            return []

        agents = []
        try:
            for agent_file in agents_dir.glob("*.json"):
                agent_data = self._read_json(agent_file)
                if agent_data:
                    agents.append(agent_data)
        except PermissionError:
            # Try sudo fallback for NFS permission issues
            agents = self._list_json_files_sudo(agents_dir)

        return agents

    # Job metadata methods
    def save_job(self, job_name: str, job_config: dict[str, Any]) -> bool:
        """Save job configuration to repository (thread-safe).

        Args:
            job_name: Name of the backup job
            job_config: Job configuration
        """
        self._ensure_dirs()

        job_dir = self.metadata_dir / "jobs" / self._safe_filename(job_name)
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "runs").mkdir(exist_ok=True)

        config_path = job_dir / "config.json"

        try:
            existing = self._read_json(config_path) or {}
            now = datetime.now().isoformat()
            return self._write_json(
                config_path,
                {
                    "schema_version": "2",
                    "job_name": job_name,
                    "config": job_config,
                    "created_at": existing.get("created_at", now),
                    "updated_at": now,
                },
            )
        except Exception as e:
            logger.error(f"Failed to save job {job_name}: {e}")
            return False

    def get_job(self, job_name: str) -> dict[str, Any] | None:
        """Get job configuration from repository."""
        job_dir = self.metadata_dir / "jobs" / self._safe_filename(job_name)
        return self._read_json(job_dir / "config.json")

    def list_jobs(self) -> list[dict[str, Any]]:
        """List all jobs that have run against this repository."""
        jobs_dir = self.metadata_dir / "jobs"
        if not jobs_dir.exists():
            return []

        jobs = []
        try:
            for job_dir in jobs_dir.iterdir():
                if job_dir.is_dir():
                    job_data = self._read_json(job_dir / "config.json")
                    if job_data:
                        # Add run count
                        runs_dir = job_dir / "runs"
                        try:
                            if runs_dir.exists():
                                job_data["run_count"] = len(list(runs_dir.glob("*.json")))
                            else:
                                job_data["run_count"] = 0
                        except PermissionError:
                            job_data["run_count"] = 0
                        jobs.append(job_data)
        except PermissionError:
            # Try sudo fallback for NFS permission issues
            job_dirs = self._list_dirs_sudo(jobs_dir)
            for job_dir_name in job_dirs:
                job_dir = jobs_dir / job_dir_name
                job_data = self._read_json(job_dir / "config.json")
                if job_data:
                    job_data["run_count"] = 0  # Skip run count with sudo
                    jobs.append(job_data)

        return jobs

    # Job run methods
    def save_job_run(
        self,
        job_name: str,
        run_id: str,
        run_data: dict[str, Any],
    ) -> bool:
        """Save a job run record to repository.

        Args:
            job_name: Name of the backup job
            run_id: Unique run identifier
            run_data: Run information (status, times, bytes, etc.)
        """
        self._ensure_dirs()

        runs_dir = self.metadata_dir / "jobs" / self._safe_filename(job_name) / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)

        run_path = runs_dir / f"{self._safe_filename(run_id)}.json"

        run_data["run_id"] = run_id
        run_data["job_name"] = job_name
        run_data["recorded_at"] = datetime.now().isoformat()

        return self._write_json(run_path, run_data)

    def get_job_runs(self, job_name: str, limit: int = 50) -> list[dict[str, Any]]:
        """Get recent job runs from repository.

        Args:
            job_name: Name of the backup job
            limit: Maximum number of runs to return

        Returns:
            List of run records, most recent first
        """
        runs_dir = self.metadata_dir / "jobs" / self._safe_filename(job_name) / "runs"
        if not runs_dir.exists():
            return []

        # Read all run files and their data
        runs = []
        for run_file in runs_dir.glob("*.json"):
            run_data = self._read_json(run_file)
            if run_data:
                runs.append(run_data)

        # Sort by started_at timestamp (falling back to recorded_at, then empty string)
        # This ensures correct chronological order regardless of file modification time
        runs.sort(
            key=lambda r: r.get("started_at") or r.get("recorded_at") or "",
            reverse=True
        )

        return runs[:limit]

    def get_latest_run(self, job_name: str) -> dict[str, Any] | None:
        """Get the most recent run for a job."""
        runs = self.get_job_runs(job_name, limit=1)
        return runs[0] if runs else None

    # Snapshot methods
    def save_snapshot(self, snapshot_id: str, snapshot_data: dict[str, Any]) -> bool:
        """Save snapshot metadata to repository.

        Args:
            snapshot_id: Kopia snapshot ID
            snapshot_data: Snapshot information
        """
        self._ensure_dirs()

        # Use the first 12 chars of the snapshot ID for the filename.
        short_id = snapshot_id[:12] if len(snapshot_id) > 12 else snapshot_id
        snapshot_path = self.metadata_dir / "snapshots" / f"{short_id}.json"

        snapshot_data["snapshot_id"] = snapshot_id
        snapshot_data["recorded_at"] = datetime.now().isoformat()

        return self._write_json(snapshot_path, snapshot_data)

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        """Get snapshot metadata from repository."""
        short_id = snapshot_id[:12] if len(snapshot_id) > 12 else snapshot_id
        return self._read_json(self.metadata_dir / "snapshots" / f"{short_id}.json")

    def list_snapshots(self) -> list[dict[str, Any]]:
        """List all snapshots in repository metadata."""
        snapshots_dir = self.metadata_dir / "snapshots"
        if not snapshots_dir.exists():
            return []

        snapshots = []
        try:
            for snapshot_file in snapshots_dir.glob("*.json"):
                snapshot_data = self._read_json(snapshot_file)
                if snapshot_data:
                    snapshots.append(snapshot_data)
        except PermissionError:
            # Try sudo fallback for NFS permission issues
            snapshots = self._list_json_files_sudo(snapshots_dir)

        # Sort by time, newest first
        snapshots.sort(
            key=lambda s: s.get("time") or s.get("recorded_at") or "",
            reverse=True
        )

        return snapshots

    # Discovery methods
    def discover_all(self) -> dict[str, Any]:
        """Discover all metadata in the repository.

        Scans job subfolders for metadata since each backup job has its own
        subfolder within the repository (repo_root/job_name/.backer/).

        Returns a complete summary of what's in the repository.

        Returns:
            Dict with agents, jobs, runs, snapshots
        """
        # Scan job subfolders for metadata
        # New structure: repo_root/Agents/{job_name}/.backer/
        # Legacy structure: repo_root/{job_name}/.backer/
        all_agents: list[dict[str, Any]] = []
        all_jobs: list[dict[str, Any]] = []
        all_snapshots: list[dict[str, Any]] = []
        agent_ids_seen: set[str] = set()
        job_names_seen: set[str] = set()
        found_any = False
        root_metadata: dict[str, Any] | None = None

        def add_agent(agent: dict[str, Any]) -> None:
            agent_id = agent.get("agent_id")
            if agent_id and agent_id not in agent_ids_seen:
                agent_ids_seen.add(agent_id)
                all_agents.append(agent)

        def add_job(job: dict[str, Any]) -> None:
            job_name = job.get("job_name")
            if job_name:
                if job_name in job_names_seen:
                    return
                job_names_seen.add(job_name)
            all_jobs.append(job)

        # Root-level metadata (legacy layout, and/or the repo-level sidecar
        # that coexists with per-job sidecars under Agents/*/.backer/).
        # This used to be an early return, which hid every per-job sidecar
        # whenever a root sidecar also existed - merge instead.
        if self.is_initialized():
            found_any = True
            root_metadata = self.get_metadata() or {}
            for agent in self.list_agents():
                add_agent(agent)
            for job in self.list_jobs():
                add_job(job)
            all_snapshots.extend(self.list_snapshots())

        def scan_folder(folder: Path, prefix: str = "") -> None:
            """Scan a folder for job metadata."""
            nonlocal found_any
            if not folder.exists():
                return

            # Try normal iteration, fall back to sudo ls
            try:
                items = list(folder.iterdir())
            except PermissionError:
                item_names = self._list_dirs_sudo(folder)
                items = [folder / name for name in item_names]

            for item in items:
                try:
                    is_dir = item.is_dir()
                except PermissionError:
                    continue  # Skip items we can't stat

                if is_dir and not item.name.startswith('.'):
                    # Check if this subfolder has .backer metadata
                    subfolder_meta = RepositoryMetadata(item, self.repo_type)
                    if subfolder_meta.is_initialized():
                        found_any = True
                        folder_path = f"{prefix}{item.name}" if prefix else item.name
                        logger.debug(f"Found metadata in job folder: {folder_path}")

                        # Aggregate agents (dedup by agent_id)
                        for agent in subfolder_meta.list_agents():
                            add_agent(agent)

                        # Aggregate jobs (dedup by job_name)
                        for job in subfolder_meta.list_jobs():
                            job["job_folder"] = folder_path
                            add_job(job)

                        # Aggregate snapshots
                        for snap in subfolder_meta.list_snapshots():
                            snap["job_folder"] = folder_path
                            all_snapshots.append(snap)

        if self.repo_path.exists():
            # First scan the new structure: Agents/ folder
            agents_folder = self.repo_path / "Agents"
            if agents_folder.exists():
                scan_folder(agents_folder, "Agents/")

            # Also scan legacy structure (direct subfolders)
            # Skip Agents/ and Hypervisors/ folders as they use the new structure
            try:
                items = list(self.repo_path.iterdir())
            except PermissionError:
                item_names = self._list_dirs_sudo(self.repo_path)
                items = [self.repo_path / name for name in item_names]

            skip_dirs = ('.', '..', 'Agents', 'Hypervisors')
            for item in items:
                try:
                    is_dir = item.is_dir()
                except PermissionError:
                    continue

                if is_dir and item.name not in skip_dirs and not item.name.startswith('.'):
                    subfolder_meta = RepositoryMetadata(item, self.repo_type)
                    if subfolder_meta.is_initialized():
                        found_any = True
                        logger.debug(f"Found metadata in legacy job folder: {item.name}")

                        for agent in subfolder_meta.list_agents():
                            add_agent(agent)

                        for job in subfolder_meta.list_jobs():
                            job["job_folder"] = item.name
                            add_job(job)

                        for snap in subfolder_meta.list_snapshots():
                            snap["job_folder"] = item.name
                            all_snapshots.append(snap)

        if not found_any:
            return {
                "initialized": False,
                "agents": [],
                "jobs": [],
                "snapshots": [],
            }

        total_runs = sum(job.get("run_count", 0) for job in all_jobs)

        result: dict[str, Any] = {
            "initialized": True,
            "agents": all_agents,
            "jobs": all_jobs,
            "snapshots": all_snapshots,
            "summary": {
                "agent_count": len(all_agents),
                "job_count": len(all_jobs),
                "snapshot_count": len(all_snapshots),
                "total_runs": total_runs,
            },
        }
        if root_metadata is not None:
            result["metadata"] = root_metadata
        return result

    def _safe_filename(self, name: str) -> str:
        """Convert a name to a safe filename."""
        # Replace problematic characters
        safe = name.replace("/", "_").replace("\\", "_").replace(":", "_")
        safe = safe.replace("<", "_").replace(">", "_").replace("|", "_")
        safe = safe.replace("?", "_").replace("*", "_").replace('"', "_")
        return safe
