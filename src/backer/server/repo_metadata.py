"""Server-side repository scanning and metadata import.

This module imports repository metadata into the server database.

For the core RepositoryMetadata class, see backer.core.repo_metadata.
"""

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

# Import shared classes from core
from backer.core.repo_metadata import (
    BACKER_METADATA_DIR,
    METADATA_VERSION,
    RepositoryMetadata,
    RepositoryMetadataError,
)
from backer.server import timezone as tz

if TYPE_CHECKING:
    from backer.server.storage import Storage

logger = logging.getLogger(__name__)

# Re-export for backwards compatibility
__all__ = [
    "BACKER_METADATA_DIR",
    "METADATA_VERSION",
    "RepositoryMetadata",
    "RepositoryMetadataError",
    "import_repository_metadata",
]


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

    # Use discover_all() which scans both root-level and job subfolder metadata
    # This handles the new structure: repo_root/Agents/{job_name}/.backer/
    discovery = repo_meta.discover_all()

    # Check if any metadata was found (either at root level or in subfolders)
    if not discovery.get("initialized"):
        return {"error": "Repository has no Backer metadata"}

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
                # Agents need their secrets before we can import them.
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
                config.pop("backend", None)
                config.pop("backend_type", None)
                # Update config to reference this repository
                config["repository_id"] = repo_id
                config["imported_at"] = tz.get_now().isoformat()
                config["imported_from_repo"] = True

                storage.save_job(job_name, config)
                imported["jobs"] += 1

                # Import job runs
                runs = repo_meta.get_job_runs(job_name)
                for run in runs:
                    run_id = run.get("run_id")
                    if run_id:
                        # Parse timestamps with error handling for malformed dates
                        started_at = tz.get_now()
                        finished_at = None

                        if run.get("started_at"):
                            try:
                                started_at = datetime.fromisoformat(run["started_at"])
                            except (ValueError, TypeError):
                                logger.warning(f"Invalid started_at format for run {run_id}: {run.get('started_at')}")

                        if run.get("finished_at"):
                            try:
                                finished_at = datetime.fromisoformat(run["finished_at"])
                            except (ValueError, TypeError):
                                logger.warning(f"Invalid finished_at format for run {run_id}: {run.get('finished_at')}")

                        storage.save_job_run(
                            run_id=run_id,
                            job_name=job_name,
                            status=run.get("status", "unknown"),
                            started_at=started_at,
                            finished_at=finished_at,
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
