"""Hypervisor backup discovery and adoption service.

Scans repositories for orphaned hypervisor backups and provides
adoption functionality for disaster recovery scenarios.
"""

import json
import logging
from pathlib import Path
from typing import Any

from backer.hypervisors.metadata import HypervisorMetadata
from backer.server.storage import Storage

logger = logging.getLogger(__name__)


class HypervisorDiscoveryService:
    """Service for discovering and adopting orphaned hypervisor backups."""

    def __init__(
        self,
        repo_path: Path | str,
        repo_type: str,
        storage: Storage,
        server: str | None = None,
        share: str | None = None,
        username: str | None = None,
        password: str | None = None,
        domain: str | None = None,
    ):
        """Initialize discovery service.

        Args:
            repo_path: Path to repository root (for local/mounted) or subpath (for SMB)
            repo_type: Repository type (smb, nfs, local)
            storage: Database storage instance
            server: SMB/NFS server (required for smb/nfs on Linux)
            share: SMB share or NFS export (required for smb/nfs on Linux)
            username: SMB username (optional)
            password: SMB password (optional)
            domain: SMB domain (optional)
        """
        self.repo_path = Path(repo_path) if isinstance(repo_path, str) else repo_path
        self.repo_type = repo_type
        self.storage = storage
        self.server = server
        self.share = share
        self.username = username
        self.password = password
        self.domain = domain

    def _use_smb_client(self) -> bool:
        """Check if we should use SMB client for file access."""
        import sys
        return self.repo_type == "smb" and sys.platform != 'win32' and self.server and self.share

    def _smb_list_folders(self, remote_path: str) -> list[str]:
        """List folders in SMB share using smbclient."""
        from backer.server.repositories import smb_list_files

        success, result = smb_list_files(
            self.server,
            self.share,
            remote_path,
            self.username,
            self.password,
            self.domain
        )

        if not success:
            logger.warning(f"[DISCOVERY] Failed to list SMB folder {remote_path}: {result}")
            return []

        logger.debug(f"[DISCOVERY] SMB list for {remote_path}: {result}")
        # Filter to only directories (entries without extensions typically)
        # This is a heuristic - SMB list doesn't distinguish dirs from files
        filtered = [name for name in result if not name.startswith('.')]
        logger.debug(f"[DISCOVERY] After filtering: {filtered}")
        return filtered

    def _smb_read_json(self, remote_path: str) -> dict[str, Any] | None:
        """Read and parse JSON file from SMB share."""
        from backer.server.repositories import smb_read_file

        success, content = smb_read_file(
            self.server,
            self.share,
            remote_path,
            self.username,
            self.password,
            self.domain
        )

        if not success:
            return None

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse JSON from {remote_path}")
            return None

    def _smb_write_json(self, remote_path: str, data: dict[str, Any]) -> bool:
        """Write JSON data to a file on SMB share."""
        from backer.server.repositories import smb_write_file

        try:
            content = json.dumps(data, indent=2)
            success, message = smb_write_file(
                self.server,
                self.share,
                remote_path,
                content,
                self.username,
                self.password,
                self.domain
            )

            if not success:
                logger.warning(f"Failed to write JSON to {remote_path}: {message}")
                return False

            return True

        except Exception as e:
            logger.exception(f"Error writing JSON to {remote_path}: {e}")
            return False

    def _scan_smb_guests(self, hypervisors_path: str, hv_folder_name: str) -> list[dict[str, Any]]:
        """Scan guests for a hypervisor using SMB client."""
        guests = []
        metadata_path = f"{hypervisors_path}/{hv_folder_name}/.backer/hypervisor_backups"

        # List guest folders (VMIDs)
        vmid_folders = self._smb_list_folders(metadata_path)
        logger.debug(f"[DISCOVERY] Found {len(vmid_folders)} guest folders in {hv_folder_name}: {vmid_folders}")

        for vmid_str in vmid_folders:
            logger.debug(f"[DISCOVERY] Reading guest from folder '{vmid_str}'")
            guest_json_path = f"{metadata_path}/{vmid_str}/guest.json"
            guest_data = self._smb_read_json(guest_json_path)

            if guest_data:
                actual_vmid = guest_data.get('vmid')
                logger.info(f"[DISCOVERY] Found guest: folder='{vmid_str}', vmid={actual_vmid}, name={guest_data.get('name')}")
                if str(actual_vmid) != vmid_str:
                    logger.warning(f"[DISCOVERY] MISMATCH: Folder name '{vmid_str}' != vmid '{actual_vmid}'")

                # Log full guest data to debug VMID issues
                logger.debug(f"[DISCOVERY] Full guest.json content: {json.dumps(guest_data, indent=2)}")
                guests.append(guest_data)
            else:
                logger.warning(f"[DISCOVERY] Failed to read or parse guest.json from folder '{vmid_str}'")

        return guests

    def _scan_smb_jobs(self, hypervisors_path: str, hv_folder_name: str) -> list[dict[str, Any]]:
        """Scan jobs for a hypervisor using SMB client."""
        jobs = []
        jobs_path = f"{hypervisors_path}/{hv_folder_name}/.backer/jobs"

        # List job JSON files
        job_files = self._smb_list_folders(jobs_path)

        for job_file in job_files:
            if not job_file.endswith('.json'):
                continue

            job_json_path = f"{jobs_path}/{job_file}"
            job_data = self._smb_read_json(job_json_path)

            if job_data:
                jobs.append(job_data)

        return jobs

    def _get_smb_backup_runs(self, hypervisors_path: str, hv_folder_name: str, vmid_str: str) -> list[dict[str, Any]]:
        """Get backup runs for a guest using SMB client."""
        runs_path = f"{hypervisors_path}/{hv_folder_name}/.backer/hypervisor_backups/{vmid_str}/runs"

        # List all JSON files in runs directory (each file is one run)
        run_files = self._smb_list_folders(runs_path)

        runs = []
        for run_file in run_files:
            if not run_file.endswith('.json'):
                continue

            run_json_path = f"{runs_path}/{run_file}"
            run_data = self._smb_read_json(run_json_path)

            if run_data:
                runs.append(run_data)

        # Sort by started_at descending
        runs.sort(key=lambda x: x.get("started_at", ""), reverse=True)
        return runs[:50]  # Limit to 50 most recent

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
        logger.info(f"Discovering orphaned backups in repository")

        # Get all active hypervisor IDs from database
        active_hypervisors = self.storage.list_hypervisors()
        active_hypervisor_ids = {hv["id"] for hv in active_hypervisors}
        logger.debug(f"Active hypervisor IDs: {active_hypervisor_ids}")

        #Scan all hypervisor folders in Hypervisors/ directory
        all_guests = []
        all_jobs = []
        vmid_to_hv_folder = {}  # Map vmid -> hypervisor folder name for later lookups

        if self._use_smb_client():
            # Use SMB client to scan remote share
            logger.debug("Using SMB client for discovery")
            base_path = str(self.repo_path) if self.repo_path != Path('.') else ""
            hypervisors_path = f"{base_path}/Hypervisors" if base_path else "Hypervisors"

            # List hypervisor folders
            hv_folders = self._smb_list_folders(hypervisors_path)
            logger.debug(f"Found {len(hv_folders)} hypervisor folders in SMB share")

            for hv_folder_name in hv_folders:
                if hv_folder_name.startswith('.'):
                    continue

                # Read guests for this hypervisor
                guests = self._scan_smb_guests(hypervisors_path, hv_folder_name)
                all_guests.extend(guests)

                # Map VMIDs to this hypervisor folder
                for guest in guests:
                    vmid = guest.get("vmid")
                    if vmid is not None:
                        vmid_to_hv_folder[str(vmid)] = hv_folder_name

                # Read jobs for this hypervisor
                jobs = self._scan_smb_jobs(hypervisors_path, hv_folder_name)
                all_jobs.extend(jobs)

        else:
            # Use local filesystem access
            logger.debug("Using local filesystem for discovery")
            hypervisors_dir = self.repo_path / "Hypervisors"

            if hypervisors_dir.exists() and hypervisors_dir.is_dir():
                for hv_folder in hypervisors_dir.iterdir():
                    if hv_folder.is_dir() and not hv_folder.name.startswith('.'):
                        # Each folder represents a hypervisor's backup location
                        hv_metadata = HypervisorMetadata(hv_folder)

                        # Collect guests from this hypervisor folder
                        guests = hv_metadata.list_guests()
                        all_guests.extend(guests)

                        # Map vmid to hypervisor folder name
                        for guest in guests:
                            vmid = guest.get("vmid")
                            if vmid is not None:
                                vmid_to_hv_folder[str(vmid)] = hv_folder.name

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
                    # Get backup runs for this guest
                    vmid_str = str(vmid)
                    hv_folder_name = vmid_to_hv_folder.get(vmid_str)

                    if hv_folder_name:
                        if self._use_smb_client():
                            base_path = str(self.repo_path) if self.repo_path != Path('.') else ""
                            hypervisors_path = f"{base_path}/Hypervisors" if base_path else "Hypervisors"
                            runs = self._get_smb_backup_runs(hypervisors_path, hv_folder_name, vmid_str)
                        else:
                            hypervisors_dir = self.repo_path / "Hypervisors"
                            hv_folder_path = hypervisors_dir / hv_folder_name
                            hv_metadata = HypervisorMetadata(hv_folder_path)
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
        # Get deleted job IDs - for SMB this is simplified (we already filtered in scan)
        deleted_job_ids = set()
        if not self._use_smb_client():
            hypervisors_dir = self.repo_path / "Hypervisors"
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
        repository_id: str | None = None,
    ) -> dict[str, Any]:
        """Adopt orphaned backups to a new hypervisor.

        Updates guest metadata files to link to the new hypervisor and
        optionally imports job configurations into the database.

        Args:
            new_hypervisor_id: ID of hypervisor to adopt backups to
            new_hypervisor_name: Name of the hypervisor (for logging)
            guest_vmids: List of VM IDs to adopt
            import_jobs: Whether to import job configurations
            repository_id: Repository ID (for auto-creating jobs)

        Returns:
            Dictionary with adoption results
        """
        from datetime import datetime

        logger.info(
            f"Adopting {len(guest_vmids)} guests to hypervisor '{new_hypervisor_name}' ({new_hypervisor_id})"
        )

        adopted_guests = 0
        adopted_jobs = 0
        total_backups_linked = 0
        errors = []

        if self._use_smb_client():
            # Use SMB client for adoption
            logger.debug("Using SMB client for adoption")
            base_path = str(self.repo_path) if self.repo_path != Path('.') else ""
            hypervisors_path = f"{base_path}/Hypervisors" if base_path else "Hypervisors"

            # Build a map of vmid -> hypervisor_folder by scanning all hypervisor folders
            vmid_to_hv_folder = {}
            hv_folders = self._smb_list_folders(hypervisors_path)

            for hv_folder_name in hv_folders:
                if hv_folder_name.startswith('.'):
                    continue

                # Read guests for this hypervisor
                guests = self._scan_smb_guests(hypervisors_path, hv_folder_name)
                for guest in guests:
                    vmid = guest.get("vmid")
                    if vmid is not None:
                        vmid_to_hv_folder[str(vmid)] = hv_folder_name

            # Update guest metadata for each VMID
            for vmid in guest_vmids:
                try:
                    vmid_str = str(vmid)

                    # Find which hypervisor folder contains this VMID
                    if vmid_str not in vmid_to_hv_folder:
                        logger.warning(f"Guest metadata not found for VMID {vmid}, skipping")
                        errors.append(f"Guest {vmid}: metadata not found")
                        continue

                    hv_folder_name = vmid_to_hv_folder[vmid_str]
                    guest_json_path = f"{hypervisors_path}/{hv_folder_name}/.backer/hypervisor_backups/{vmid_str}/guest.json"

                    # Read current guest metadata
                    guest_data = self._smb_read_json(guest_json_path)
                    if not guest_data:
                        logger.warning(f"Failed to read guest metadata for VMID {vmid}, skipping")
                        errors.append(f"Guest {vmid}: failed to read metadata")
                        continue

                    # Update hypervisor link
                    old_hypervisor_id = guest_data.get("hypervisor_id")
                    guest_data["hypervisor_id"] = new_hypervisor_id
                    guest_data["updated_at"] = datetime.now().isoformat()
                    guest_data["hypervisor_name"] = new_hypervisor_name

                    # Record adoption history
                    if "adoption_history" not in guest_data:
                        guest_data["adoption_history"] = []

                    guest_data["adoption_history"].append({
                        "from_hypervisor_id": old_hypervisor_id,
                        "to_hypervisor_id": new_hypervisor_id,
                        "adopted_at": datetime.now().isoformat(),
                    })

                    # Write updated metadata
                    success = self._smb_write_json(guest_json_path, guest_data)
                    if success:
                        adopted_guests += 1

                        # Count backups for this guest
                        runs = self._get_smb_backup_runs(hypervisors_path, hv_folder_name, vmid_str)
                        total_backups_linked += len(runs)

                        logger.debug(f"Adopted guest {vmid} ({guest_data.get('name')}) with {len(runs)} backups")
                    else:
                        errors.append(f"Guest {vmid}: failed to write updated metadata")
                        logger.error(f"Failed to write updated metadata for guest {vmid}")

                except Exception as e:
                    logger.exception(f"Error adopting guest {vmid}")
                    errors.append(f"Guest {vmid}: {str(e)}")

        else:
            # Use local filesystem access
            logger.debug("Using local filesystem for adoption")
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

        # If no jobs were imported/adopted, auto-create a basic disabled job for convenience
        auto_created_job = False
        if adopted_jobs == 0 and adopted_guests > 0 and repository_id:
            try:
                auto_created_job = self._auto_create_adoption_job(
                    new_hypervisor_id, new_hypervisor_name, guest_vmids, repository_id
                )
                if auto_created_job:
                    adopted_jobs = 1
            except Exception as e:
                logger.exception("Failed to auto-create job")
                errors.append(f"Auto-create job failed: {str(e)}")

        result = {
            "adopted_guests": adopted_guests,
            "adopted_jobs": adopted_jobs,
            "total_backups_linked": total_backups_linked,
            "errors": errors,
            "auto_created_job": auto_created_job,
            "message": self._format_adoption_message(
                adopted_guests, adopted_jobs, total_backups_linked, auto_created_job
            ),
        }

        logger.info(
            f"Adoption complete: {adopted_guests} guests, {adopted_jobs} jobs, "
            f"{total_backups_linked} backups (auto-created: {auto_created_job})"
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

            except Exception:
                logger.exception(f"Failed to import job {job_id}")
                # Continue with other jobs

        return imported_count

    def _auto_create_adoption_job(
        self,
        hypervisor_id: str,
        hypervisor_name: str,
        guest_vmids: list[int | str],
        repository_id: str,
    ) -> bool:
        """Auto-create a basic disabled backup job for adopted VMs.

        Creates a convenient starting point job that users can enable/configure.

        Args:
            hypervisor_id: ID of the hypervisor
            hypervisor_name: Name of the hypervisor
            guest_vmids: List of adopted VM IDs
            repository_id: Repository ID to use for the job

        Returns:
            True if job was created successfully
        """
        import uuid

        # Create a basic job
        job_id = str(uuid.uuid4())
        vm_count = len(guest_vmids)
        job_name = f"Adopted VMs from {hypervisor_name}" if vm_count > 1 else f"Adopted VM from {hypervisor_name}"

        try:
            self.storage.add_hypervisor_job(
                job_id=job_id,
                name=job_name,
                hypervisor_id=hypervisor_id,
                guest_ids=[int(g) if str(g).isdigit() else g for g in guest_vmids],
                repository_id=repository_id,
                backup_mode="full",
                compression="zstd",
                schedule_cron=None,  # Manual schedule
                enabled=False,  # Disabled by default
                copies_to_keep=7,
                verify_backup=False,
            )

            logger.info(
                f"Auto-created disabled job '{job_name}' ({job_id}) for {vm_count} adopted VM(s)"
            )
            return True

        except Exception:
            logger.exception(f"Failed to auto-create job for adopted VMs")
            return False

    def _format_adoption_message(
        self, guests: int, jobs: int, backups: int, auto_created: bool = False
    ) -> str:
        """Format a user-friendly adoption success message.

        Args:
            guests: Number of guests adopted
            jobs: Number of jobs imported
            backups: Number of backups linked
            auto_created: Whether a job was auto-created

        Returns:
            Formatted message string
        """
        parts = []

        if guests > 0:
            parts.append(f"{guests} VM{'s' if guests != 1 else ''}")

        if jobs > 0:
            if auto_created:
                parts.append(f"{jobs} job (disabled - review and enable in Jobs tab)")
            else:
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
            return "Similar name pattern"
        elif score >= 70:
            return "Strong similarity"
        elif score >= 50:
            return "Possible match"
        else:
            return "Weak match"
