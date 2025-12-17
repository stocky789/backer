"""Hypervisor backup discovery and adoption service.

Scans repositories for orphaned hypervisor backups and provides
adoption functionality for disaster recovery scenarios.
"""

import logging
from pathlib import Path
from typing import Any

from backer.hypervisors.metadata import HypervisorMetadata
from backer.server.storage import Storage

logger = logging.getLogger(__name__)


class HypervisorDiscoveryService:
    """Service for discovering and adopting orphaned hypervisor backups."""

    def __init__(self, repo_path: Path | str, repo_type: str, storage: Storage):
        """Initialize discovery service.

        Args:
            repo_path: Path to repository root
            repo_type: Repository type (smb, nfs, local)
            storage: Database storage instance
        """
        self.repo_path = Path(repo_path) if isinstance(repo_path, str) else repo_path
        self.repo_type = repo_type
        self.storage = storage

    def discover_orphaned_backups(self) -> dict[str, Any]:
        """Scan repository for backups not linked to active hypervisors.

        Identifies VMs whose hypervisor_id in guest.json does not match
        any currently configured hypervisor in the database.

        Returns:
            Dictionary containing:
            - orphaned_guests: List of orphaned VM metadata
            - orphaned_jobs: List of orphaned job configurations
            - suggested_matches: List of suggested hypervisor matches
            - summary: Statistics about orphaned backups
        """
        logger.info(f"Discovering orphaned backups in {self.repo_path}")

        # Get all active hypervisor IDs from database
        active_hypervisors = self.storage.list_hypervisors()
        active_hypervisor_ids = {hv["id"] for hv in active_hypervisors}
        logger.debug(f"Active hypervisor IDs: {active_hypervisor_ids}")

        # Scan all hypervisor folders in Hypervisors/ directory
        all_guests = []
        all_jobs = []
        vmid_to_metadata = {}  # Map vmid -> HypervisorMetadata instance
        hypervisors_dir = self.repo_path / "Hypervisors"

        if hypervisors_dir.exists() and hypervisors_dir.is_dir():
            for hv_folder in hypervisors_dir.iterdir():
                if hv_folder.is_dir() and not hv_folder.name.startswith('.'):
                    # Each folder represents a hypervisor's backup location
                    hv_metadata = HypervisorMetadata(hv_folder)

                    # Collect guests from this hypervisor folder
                    guests = hv_metadata.list_guests()
                    all_guests.extend(guests)

                    # Map vmid to metadata instance for later use
                    for guest in guests:
                        vmid = guest.get("vmid")
                        if vmid is not None:
                            vmid_to_metadata[str(vmid)] = hv_metadata

                    # Collect jobs from this hypervisor folder
                    jobs = hv_metadata.list_jobs()
                    all_jobs.extend(jobs)

        logger.debug(f"Found {len(all_guests)} total guests in repository")

        # Find orphaned guests (hypervisor_id not in active set)
        orphaned_guests = []
        for guest in all_guests:
            hypervisor_id = guest.get("hypervisor_id")
            if not hypervisor_id or hypervisor_id not in active_hypervisor_ids:
                # This guest is orphaned - its hypervisor no longer exists
                vmid = guest.get("vmid")
                if vmid is not None:
                    # Get backup runs for this guest from the correct metadata instance
                    vmid_str = str(vmid)
                    if vmid_str in vmid_to_metadata:
                        hv_metadata = vmid_to_metadata[vmid_str]
                        runs = hv_metadata.get_backup_runs(vmid)
                    else:
                        runs = []

                    # Calculate total size and find latest backup
                    total_size = sum(run.get("size_bytes", 0) for run in runs)
                    latest_backup = runs[0].get("started_at") if runs else None
                    first_backup = runs[-1].get("started_at") if runs else None

                    orphaned_guests.append({
                        "vmid": vmid,
                        "name": guest.get("name", "Unknown"),
                        "guest_type": guest.get("guest_type", "unknown"),
                        "hypervisor_id": hypervisor_id,
                        "hypervisor_name": guest.get("hypervisor_name", "Unknown"),
                        "node": guest.get("node"),
                        "run_count": len(runs),
                        "latest_backup": latest_backup,
                        "first_backup": first_backup,
                        "total_size_bytes": total_size,
                    })

        logger.info(f"Found {len(orphaned_guests)} orphaned guests")

        # Process orphaned jobs from all collected jobs
        # Get deleted job IDs from all hypervisor folders
        deleted_job_ids = set()
        if hypervisors_dir.exists() and hypervisors_dir.is_dir():
            for hv_folder in hypervisors_dir.iterdir():
                if hv_folder.is_dir() and not hv_folder.name.startswith('.'):
                    hv_metadata = HypervisorMetadata(hv_folder)
                    deleted_job_ids.update(hv_metadata.get_deleted_job_ids())

        orphaned_jobs = []
        for job in all_jobs:
            job_id = job.get("job_id")
            hypervisor_id = job.get("hypervisor_id")

            # Skip deleted jobs
            if job_id in deleted_job_ids:
                continue

            # Check if job's hypervisor is orphaned
            if not hypervisor_id or hypervisor_id not in active_hypervisor_ids:
                orphaned_jobs.append({
                    "job_id": job_id,
                    "name": job.get("name", "Unknown"),
                    "hypervisor_id": hypervisor_id,
                    "repository_id": job.get("repository_id"),
                    "guest_ids": job.get("guest_ids", []),
                    "backup_mode": job.get("backup_mode"),
                    "compression": job.get("compression"),
                    "schedule_cron": job.get("schedule_cron"),
                    "enabled": job.get("enabled", True),
                    "copies_to_keep": job.get("copies_to_keep", 0),
                    "created_at": job.get("created_at"),
                })

        logger.info(f"Found {len(orphaned_jobs)} orphaned jobs")

        # Calculate summary statistics
        total_backups = sum(g["run_count"] for g in orphaned_guests)
        total_size = sum(g["total_size_bytes"] for g in orphaned_guests)

        # Suggest hypervisor matches based on naming patterns
        suggested_matches = self._suggest_hypervisor_matches(
            orphaned_guests, active_hypervisors
        )

        return {
            "orphaned_guests": orphaned_guests,
            "orphaned_jobs": orphaned_jobs,
            "suggested_matches": suggested_matches,
            "summary": {
                "total_guests": len(orphaned_guests),
                "total_jobs": len(orphaned_jobs),
                "total_backups": total_backups,
                "total_size_bytes": total_size,
            },
        }

    def adopt_backups(
        self,
        new_hypervisor_id: str,
        new_hypervisor_name: str,
        guest_vmids: list[int | str],
        import_jobs: bool = True,
    ) -> dict[str, Any]:
        """Adopt orphaned backups to a new hypervisor.

        Updates guest metadata files to link to the new hypervisor and
        optionally imports job configurations into the database.

        Args:
            new_hypervisor_id: ID of hypervisor to adopt backups to
            new_hypervisor_name: Name of the hypervisor (for logging)
            guest_vmids: List of VM IDs to adopt
            import_jobs: Whether to import job configurations

        Returns:
            Dictionary with adoption results
        """
        logger.info(
            f"Adopting {len(guest_vmids)} guests to hypervisor '{new_hypervisor_name}' ({new_hypervisor_id})"
        )

        adopted_guests = 0
        adopted_jobs = 0
        total_backups_linked = 0
        errors = []

        # Build a map of vmid -> hypervisor_folder by scanning all hypervisor folders
        hypervisors_dir = self.repo_path / "Hypervisors"
        vmid_to_folder = {}

        if hypervisors_dir.exists() and hypervisors_dir.is_dir():
            for hv_folder in hypervisors_dir.iterdir():
                if hv_folder.is_dir() and not hv_folder.name.startswith('.'):
                    hv_metadata = HypervisorMetadata(hv_folder)
                    guests = hv_metadata.list_guests()
                    for guest in guests:
                        vmid = guest.get("vmid")
                        if vmid is not None:
                            vmid_to_folder[str(vmid)] = hv_folder

        # Update guest metadata for each VMID
        for vmid in guest_vmids:
            try:
                vmid_str = str(vmid)

                # Find which hypervisor folder contains this VMID
                if vmid_str not in vmid_to_folder:
                    logger.warning(f"Guest metadata not found for VMID {vmid}, skipping")
                    errors.append(f"Guest {vmid}: metadata not found")
                    continue

                hv_folder = vmid_to_folder[vmid_str]
                hv_metadata = HypervisorMetadata(hv_folder)

                # Get current guest metadata
                guest = hv_metadata.get_guest(vmid)
                if not guest:
                    logger.warning(f"Guest metadata not found for VMID {vmid}, skipping")
                    errors.append(f"Guest {vmid}: metadata not found")
                    continue

                # Update hypervisor_id in guest.json
                success = hv_metadata.update_guest_hypervisor(vmid, new_hypervisor_id, new_hypervisor_name)
                if success:
                    adopted_guests += 1

                    # Count backups for this guest
                    runs = hv_metadata.get_backup_runs(vmid)
                    total_backups_linked += len(runs)

                    logger.debug(f"Adopted guest {vmid} ({guest.get('name')}) with {len(runs)} backups")
                else:
                    errors.append(f"Guest {vmid}: failed to update metadata")
                    logger.error(f"Failed to update metadata for guest {vmid}")

            except Exception as e:
                logger.exception(f"Error adopting guest {vmid}")
                errors.append(f"Guest {vmid}: {str(e)}")

        # Import orphaned jobs if requested
        if import_jobs:
            try:
                adopted_jobs = self._import_orphaned_jobs(new_hypervisor_id, guest_vmids)
            except Exception as e:
                logger.exception("Error importing jobs")
                errors.append(f"Job import failed: {str(e)}")

        result = {
            "adopted_guests": adopted_guests,
            "adopted_jobs": adopted_jobs,
            "total_backups_linked": total_backups_linked,
            "errors": errors,
            "message": self._format_adoption_message(
                adopted_guests, adopted_jobs, total_backups_linked
            ),
        }

        logger.info(
            f"Adoption complete: {adopted_guests} guests, {adopted_jobs} jobs, "
            f"{total_backups_linked} backups"
        )

        return result

    def _import_orphaned_jobs(
        self, new_hypervisor_id: str, adopted_vmids: list[int | str]
    ) -> int:
        """Import orphaned job configurations into database.

        Args:
            new_hypervisor_id: ID of the new hypervisor
            adopted_vmids: List of adopted VM IDs

        Returns:
            Number of jobs imported
        """
        logger.debug("Importing orphaned jobs")

        # Scan all hypervisor folders to collect jobs
        hypervisors_dir = self.repo_path / "Hypervisors"
        all_jobs = []
        deleted_job_ids = set()

        if hypervisors_dir.exists() and hypervisors_dir.is_dir():
            for hv_folder in hypervisors_dir.iterdir():
                if hv_folder.is_dir() and not hv_folder.name.startswith('.'):
                    hv_metadata = HypervisorMetadata(hv_folder)
                    jobs = hv_metadata.list_jobs()
                    all_jobs.extend(jobs)
                    deleted_job_ids.update(hv_metadata.get_deleted_job_ids())

        # Get hypervisor details from database
        hypervisor = self.storage.get_hypervisor(new_hypervisor_id)
        if not hypervisor:
            logger.error(f"Hypervisor {new_hypervisor_id} not found in database")
            return 0

        imported_count = 0
        adopted_vmids_set = set(str(v) for v in adopted_vmids)  # Normalize to strings

        for job in all_jobs:
            job_id = job.get("job_id")

            # Skip deleted jobs
            if job_id in deleted_job_ids:
                logger.debug(f"Skipping deleted job {job_id}")
                continue

            # Check if job's guest_ids overlap with adopted VMs
            job_guest_ids = job.get("guest_ids") or []
            job_guest_ids_str = set(str(g) for g in job_guest_ids)

            if not job_guest_ids_str.intersection(adopted_vmids_set):
                # This job doesn't include any of our adopted VMs
                continue

            # Check if job already exists in database
            existing_job = self.storage.get_hypervisor_job(job_id)
            if existing_job:
                logger.debug(f"Job {job_id} already exists in database, skipping")
                continue

            try:
                # Get repository ID (should match the one we're scanning)
                repository_id = job.get("repository_id")
                if not repository_id:
                    logger.warning(f"Job {job_id} has no repository_id, skipping")
                    continue

                # Create job in database
                self.storage.add_hypervisor_job(
                    job_id=job_id,
                    name=job.get("name", f"Imported Job {job_id[:8]}"),
                    hypervisor_id=new_hypervisor_id,
                    guest_ids=[int(g) if str(g).isdigit() else g for g in job_guest_ids],
                    repository_id=repository_id,
                    backup_mode=job.get("backup_mode", "snapshot"),
                    compression=job.get("compression", "zstd"),
                    schedule_cron=job.get("schedule_cron"),
                    enabled=job.get("enabled", True),
                    copies_to_keep=job.get("copies_to_keep", 0),
                    verify_backup=job.get("verify_backup", False),
                )

                imported_count += 1
                logger.info(f"Imported job {job_id} ({job.get('name')})")

            except Exception as e:
                logger.exception(f"Failed to import job {job_id}")
                # Continue with other jobs

        return imported_count

    def _format_adoption_message(
        self, guests: int, jobs: int, backups: int
    ) -> str:
        """Format a user-friendly adoption success message.

        Args:
            guests: Number of guests adopted
            jobs: Number of jobs imported
            backups: Number of backups linked

        Returns:
            Formatted message string
        """
        parts = []

        if guests > 0:
            parts.append(f"{guests} VM{'s' if guests != 1 else ''}")

        if jobs > 0:
            parts.append(f"{jobs} job{'s' if jobs != 1 else ''}")

        if backups > 0:
            parts.append(f"{backups} backup{'s' if backups != 1 else ''}")

        if not parts:
            return "No items were adopted"

        return "Successfully adopted " + ", ".join(parts)

    def _suggest_hypervisor_matches(
        self,
        orphaned_guests: list[dict[str, Any]],
        active_hypervisors: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Suggest hypervisor matches based on naming patterns.

        Analyzes orphaned guest metadata and active hypervisors to suggest
        likely matches based on hostname, hypervisor name, and node patterns.

        Args:
            orphaned_guests: List of orphaned guest metadata
            active_hypervisors: List of active hypervisors from database

        Returns:
            List of suggested matches with confidence scores
        """
        if not orphaned_guests or not active_hypervisors:
            return []

        suggestions = []

        # Group orphaned guests by their original hypervisor
        orphaned_by_hv = {}
        for guest in orphaned_guests:
            hv_id = guest.get("hypervisor_id", "unknown")
            hv_name = guest.get("hypervisor_name", "Unknown")
            if hv_id not in orphaned_by_hv:
                orphaned_by_hv[hv_id] = {
                    "original_id": hv_id,
                    "original_name": hv_name,
                    "guest_count": 0,
                    "guests": [],
                }
            orphaned_by_hv[hv_id]["guest_count"] += 1
            orphaned_by_hv[hv_id]["guests"].append(guest["vmid"])

        # Try to match each orphaned hypervisor to an active one
        for orphaned_hv_id, orphaned_info in orphaned_by_hv.items():
            original_name = orphaned_info["original_name"].lower()

            best_match = None
            best_score = 0

            for active_hv in active_hypervisors:
                active_name = active_hv["name"].lower()
                active_host = (active_hv.get("host") or "").lower()
                score = 0

                # Exact name match
                if original_name == active_name:
                    score += 100

                # Partial name match
                elif original_name in active_name or active_name in original_name:
                    score += 70

                # Name contains similar words
                else:
                    original_words = set(original_name.replace("-", " ").split())
                    active_words = set(active_name.replace("-", " ").split())
                    common_words = original_words & active_words
                    if common_words:
                        score += len(common_words) * 20

                # Hostname match
                if original_name in active_host or active_host in original_name:
                    score += 50

                # Type match (e.g., both are hyperv-cluster)
                if active_hv["hypervisor_type"] == "hyperv-cluster":
                    # Cluster names often contain "cluster"
                    if "cluster" in original_name and "cluster" in active_name:
                        score += 30

                if score > best_score:
                    best_score = score
                    best_match = active_hv

            # Only suggest if confidence is reasonable (>= 50)
            if best_match and best_score >= 50:
                suggestions.append({
                    "original_hypervisor_id": orphaned_hv_id,
                    "original_hypervisor_name": orphaned_info["original_name"],
                    "suggested_hypervisor_id": best_match["id"],
                    "suggested_hypervisor_name": best_match["name"],
                    "suggested_hypervisor_type": best_match["hypervisor_type"],
                    "confidence_score": best_score,
                    "guest_count": orphaned_info["guest_count"],
                    "guest_vmids": orphaned_info["guests"],
                    "reason": self._get_match_reason(
                        orphaned_info["original_name"],
                        best_match["name"],
                        best_score
                    ),
                })

        # Sort by confidence score descending
        suggestions.sort(key=lambda x: x["confidence_score"], reverse=True)

        logger.info(f"Generated {len(suggestions)} hypervisor match suggestions")
        return suggestions

    def _get_match_reason(self, original: str, suggested: str, score: int) -> str:
        """Generate human-readable reason for match suggestion.

        Args:
            original: Original hypervisor name
            suggested: Suggested hypervisor name
            score: Confidence score

        Returns:
            Reason string
        """
        original_lower = original.lower()
        suggested_lower = suggested.lower()

        if original_lower == suggested_lower:
            return "Exact name match"
        elif original_lower in suggested_lower:
            return f"Name contains '{original}'"
        elif suggested_lower in original_lower:
            return f"Similar name pattern"
        elif score >= 70:
            return "Strong similarity"
        elif score >= 50:
            return "Possible match"
        else:
            return "Weak match"