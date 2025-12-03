"""Server-side repository scanning and metadata import.

This module provides server-specific functionality for:
- Scanning restic repositories for existing snapshots
- Importing repository metadata into the server database

For the core RepositoryMetadata class, see backer.core.repo_metadata.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Import shared classes from core
from backer.core.repo_metadata import (
    BACKER_METADATA_DIR,
    METADATA_VERSION,
    RepositoryMetadata,
    RepositoryMetadataError,
)

if TYPE_CHECKING:
    from backer.server.storage import Storage

logger = logging.getLogger(__name__)

# Re-export for backwards compatibility
__all__ = [
    "BACKER_METADATA_DIR",
    "METADATA_VERSION",
    "RepositoryMetadata",
    "RepositoryMetadataError",
    "ResticRepositoryScanner",
    "import_repository_metadata",
]


class ResticRepositoryScanner:
    """Scans restic repositories to discover existing snapshots.

    This complements RepositoryMetadata by querying restic directly
    for snapshot information that may not be in our metadata.
    """

    def __init__(self, repo_path: str, password: str, restic_path: str | Path | None = None):
        """Initialize scanner.

        Args:
            repo_path: Path to restic repository
            password: Repository password
            restic_path: Optional path to restic binary
        """
        self.repo_path = repo_path
        self.password = password
        self.restic_path = restic_path or self._find_restic()

    def _find_restic(self) -> str:
        """Find restic binary."""
        # Check common locations
        if sys.platform == "win32":
            candidates = ["restic.exe", "C:\\Program Files\\Backer Agent\\tools\\restic.exe"]
        else:
            candidates = ["restic", "/usr/bin/restic", "/usr/local/bin/restic"]

        for candidate in candidates:
            if Path(candidate).exists() or self._which(candidate):
                return candidate

        return "restic"  # Hope it's in PATH

    def _which(self, cmd: str) -> str | None:
        """Find command in PATH."""
        return shutil.which(cmd)

    def _run_restic(self, *args: str) -> tuple[int, str, str]:
        """Run a restic command."""
        env = os.environ.copy()
        env["RESTIC_PASSWORD"] = self.password

        cmd = [str(self.restic_path), "-r", self.repo_path, *args]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=300,  # 5 minute timeout
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", "Command timed out"
        except Exception as e:
            return -1, "", str(e)

    def is_restic_repo(self) -> bool:
        """Check if path is a valid restic repository."""
        returncode, _, _ = self._run_restic("cat", "config")
        return returncode == 0

    def list_snapshots(self) -> list[dict[str, Any]]:
        """List all snapshots in the restic repository."""
        returncode, stdout, stderr = self._run_restic("snapshots", "--json")

        if returncode != 0:
            logger.error(f"Failed to list snapshots: {stderr}")
            return []

        try:
            snapshots = json.loads(stdout) if stdout else []
            return snapshots
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse snapshots: {e}")
            return []

    def get_snapshot_stats(self, snapshot_id: str) -> dict[str, Any] | None:
        """Get statistics for a specific snapshot."""
        returncode, stdout, stderr = self._run_restic("stats", snapshot_id, "--json")

        if returncode != 0:
            logger.warning(f"Failed to get stats for {snapshot_id}: {stderr}")
            return None

        try:
            return json.loads(stdout) if stdout else None
        except json.JSONDecodeError:
            return None

    def scan_and_sync(self, repo_metadata: RepositoryMetadata) -> dict[str, Any]:
        """Scan restic repository and sync metadata.

        Discovers all snapshots and updates the repository metadata.

        Args:
            repo_metadata: RepositoryMetadata instance to sync with

        Returns:
            Summary of discovered/synced data
        """
        if not self.is_restic_repo():
            return {"error": "Not a valid restic repository"}

        snapshots = self.list_snapshots()

        synced = 0
        for snapshot in snapshots:
            snapshot_id = snapshot.get("id") or snapshot.get("short_id")
            if not snapshot_id:
                continue

            # Check if we already have this snapshot
            existing = repo_metadata.get_snapshot(snapshot_id)
            if not existing:
                # Save new snapshot metadata
                snapshot_data = {
                    "snapshot_id": snapshot_id,
                    "short_id": snapshot.get("short_id", snapshot_id[:8]),
                    "time": snapshot.get("time"),
                    "hostname": snapshot.get("hostname"),
                    "username": snapshot.get("username"),
                    "paths": snapshot.get("paths", []),
                    "tags": snapshot.get("tags", []),
                }
                repo_metadata.save_snapshot(snapshot_id, snapshot_data)
                synced += 1

                # Try to associate with an agent
                hostname = snapshot.get("hostname")
                if hostname:
                    agents = repo_metadata.list_agents()
                    matching_agent = next(
                        (a for a in agents if a.get("hostname") == hostname),
                        None
                    )
                    if not matching_agent:
                        # Create agent entry
                        repo_metadata.save_agent(
                            agent_id=f"discovered_{hostname}",
                            agent_data={
                                "hostname": hostname,
                                "discovered": True,
                                "source": "restic_scan",
                            }
                        )

        return {
            "total_snapshots": len(snapshots),
            "new_snapshots_synced": synced,
            "already_tracked": len(snapshots) - synced,
        }


def import_repository_metadata(
    repo_path: str,
    storage: "Storage",
    repo_id: str,
) -> dict[str, Any]:
    """Import metadata from repository into server database.

    This is used when connecting a new server to an existing repository
    with Backer metadata.

    Args:
        repo_path: Path to the repository
        storage: Server Storage instance
        repo_id: Repository ID in the database

    Returns:
        Import summary
    """
    repo_meta = RepositoryMetadata(repo_path)

    if not repo_meta.is_initialized():
        return {"error": "Repository has no Backer metadata"}

    discovery = repo_meta.discover_all()

    imported = {
        "agents": 0,
        "jobs": 0,
        "runs": 0,
    }

    # Import agents (as discovered clients)
    for agent in discovery.get("agents", []):
        agent_id = agent.get("agent_id")
        if agent_id:
            # Check if agent already exists
            existing = storage.get_client(agent_id)
            if not existing:
                # Note: We can't fully import agents without their secrets
                # Just log that they exist
                logger.info(f"Discovered agent {agent_id} ({agent.get('hostname')})")
                imported["agents"] += 1

    # Import jobs
    for job in discovery.get("jobs", []):
        job_name = job.get("job_name")
        config = job.get("config", {})

        if job_name:
            # Check if job exists
            existing = storage.get_job(job_name)
            if not existing:
                # Update config to reference this repository
                config["repository_id"] = repo_id
                config["imported_at"] = datetime.now().isoformat()
                config["imported_from_repo"] = True

                storage.save_job(job_name, config)
                imported["jobs"] += 1

                # Import job runs
                runs = repo_meta.get_job_runs(job_name)
                for run in runs:
                    run_id = run.get("run_id")
                    if run_id:
                        storage.save_job_run(
                            run_id=run_id,
                            job_name=job_name,
                            status=run.get("status", "unknown"),
                            started_at=datetime.fromisoformat(run["started_at"]) if run.get("started_at") else datetime.now(),
                            finished_at=datetime.fromisoformat(run["finished_at"]) if run.get("finished_at") else None,
                            bytes_transferred=run.get("bytes_transferred", 0),
                            files_transferred=run.get("files_transferred", 0),
                            snapshot_id=run.get("snapshot_id"),
                        )
                        imported["runs"] += 1

    return {
        "success": True,
        "imported": imported,
        "discovered": discovery.get("summary", {}),
    }
