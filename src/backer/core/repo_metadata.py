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
        └── {snapshot_id}.json  # Snapshot metadata (restic)
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Backer metadata directory name
BACKER_METADATA_DIR = ".backer"
METADATA_VERSION = "1.0"


def normalize_path_for_platform(path: str) -> str:
    """Normalize a path for the current platform.

    On Windows, converts forward-slash UNC paths (//server/share) to
    backslash format (\\\\server\\share) which Windows requires.
    """
    if sys.platform == 'win32':
        # Convert forward slash UNC paths to backslash format
        if path.startswith('//'):
            return path.replace('/', '\\')
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
        # Normalize path for the current platform (handles Windows UNC paths)
        if isinstance(repo_path, str):
            repo_path = normalize_path_for_platform(repo_path)
        self.repo_path = Path(repo_path) if isinstance(repo_path, str) else repo_path
        self.repo_type = repo_type
        self.metadata_dir = self.repo_path / BACKER_METADATA_DIR
        logger.debug(f"RepositoryMetadata initialized: repo_path={self.repo_path}, metadata_dir={self.metadata_dir}")

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
        """Read a JSON file from the repository."""
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
            return None
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read {path}: {e}")
            return None

    def _write_json(self, path: Path, data: dict[str, Any]) -> bool:
        """Write a JSON file to the repository."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
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
        """Save agent metadata to repository.

        Args:
            agent_id: Unique agent identifier
            agent_data: Agent information (hostname, os_info, etc.)
        """
        self._ensure_dirs()

        agent_path = self.metadata_dir / "agents" / f"{agent_id}.json"

        # Merge with existing data if present
        existing = self._read_json(agent_path) or {}
        existing.update(agent_data)
        existing["agent_id"] = agent_id
        existing["updated_at"] = datetime.now().isoformat()
        if "first_seen" not in existing:
            existing["first_seen"] = datetime.now().isoformat()

        return self._write_json(agent_path, existing)

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        """Get agent metadata from repository."""
        return self._read_json(self.metadata_dir / "agents" / f"{agent_id}.json")

    def list_agents(self) -> list[dict[str, Any]]:
        """List all agents that have backed up to this repository."""
        agents_dir = self.metadata_dir / "agents"
        if not agents_dir.exists():
            return []

        agents = []
        for agent_file in agents_dir.glob("*.json"):
            agent_data = self._read_json(agent_file)
            if agent_data:
                agents.append(agent_data)

        return agents

    # Job metadata methods
    def save_job(self, job_name: str, job_config: dict[str, Any]) -> bool:
        """Save job configuration to repository.

        Args:
            job_name: Name of the backup job
            job_config: Job configuration
        """
        self._ensure_dirs()

        job_dir = self.metadata_dir / "jobs" / self._safe_filename(job_name)
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "runs").mkdir(exist_ok=True)

        config_path = job_dir / "config.json"

        config_data = {
            "job_name": job_name,
            "config": job_config,
            "updated_at": datetime.now().isoformat(),
        }

        existing = self._read_json(config_path)
        if existing:
            config_data["created_at"] = existing.get("created_at", datetime.now().isoformat())
        else:
            config_data["created_at"] = datetime.now().isoformat()

        return self._write_json(config_path, config_data)

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
        for job_dir in jobs_dir.iterdir():
            if job_dir.is_dir():
                job_data = self._read_json(job_dir / "config.json")
                if job_data:
                    # Add run count
                    runs_dir = job_dir / "runs"
                    if runs_dir.exists():
                        job_data["run_count"] = len(list(runs_dir.glob("*.json")))
                    else:
                        job_data["run_count"] = 0
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

    # Snapshot methods (for restic)
    def save_snapshot(self, snapshot_id: str, snapshot_data: dict[str, Any]) -> bool:
        """Save snapshot metadata to repository.

        Args:
            snapshot_id: Restic snapshot ID
            snapshot_data: Snapshot information
        """
        self._ensure_dirs()

        # Use first 12 chars of snapshot ID for filename (like restic does)
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
        for snapshot_file in snapshots_dir.glob("*.json"):
            snapshot_data = self._read_json(snapshot_file)
            if snapshot_data:
                snapshots.append(snapshot_data)

        # Sort by time, newest first
        snapshots.sort(
            key=lambda s: s.get("time") or s.get("recorded_at") or "",
            reverse=True
        )

        return snapshots

    # Discovery methods
    def discover_all(self) -> dict[str, Any]:
        """Discover all metadata in the repository.

        Returns a complete summary of what's in the repository.

        Returns:
            Dict with agents, jobs, runs, snapshots
        """
        if not self.is_initialized():
            return {
                "initialized": False,
                "agents": [],
                "jobs": [],
                "snapshots": [],
            }

        metadata = self.get_metadata() or {}
        agents = self.list_agents()
        jobs = self.list_jobs()
        snapshots = self.list_snapshots()

        # Calculate total runs
        total_runs = sum(job.get("run_count", 0) for job in jobs)

        return {
            "initialized": True,
            "metadata": metadata,
            "agents": agents,
            "jobs": jobs,
            "snapshots": snapshots,
            "summary": {
                "agent_count": len(agents),
                "job_count": len(jobs),
                "snapshot_count": len(snapshots),
                "total_runs": total_runs,
            },
        }

    def _safe_filename(self, name: str) -> str:
        """Convert a name to a safe filename."""
        # Replace problematic characters
        safe = name.replace("/", "_").replace("\\", "_").replace(":", "_")
        safe = safe.replace("<", "_").replace(">", "_").replace("|", "_")
        safe = safe.replace("?", "_").replace("*", "_").replace('"', "_")
        return safe
