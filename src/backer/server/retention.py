"""Retention policy management for backups."""

import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backer.server.storage import Storage

logger = logging.getLogger(__name__)


class RetentionPolicy:
    """Defines how long to keep backups.

    Supports multiple retention strategies:
    - keep_last: Keep the last N backups
    - keep_daily: Keep one backup per day for N days
    - keep_weekly: Keep one backup per week for N weeks
    - keep_monthly: Keep one backup per month for N months
    - max_age_days: Delete backups older than N days
    """

    def __init__(
        self,
        keep_last: int | None = None,
        keep_daily: int | None = None,
        keep_weekly: int | None = None,
        keep_monthly: int | None = None,
        max_age_days: int | None = None,
    ):
        self.keep_last = keep_last
        self.keep_daily = keep_daily
        self.keep_weekly = keep_weekly
        self.keep_monthly = keep_monthly
        self.max_age_days = max_age_days

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RetentionPolicy":
        """Create a RetentionPolicy from a dict."""
        return cls(
            keep_last=data.get("keep_last"),
            keep_daily=data.get("keep_daily"),
            keep_weekly=data.get("keep_weekly"),
            keep_monthly=data.get("keep_monthly"),
            max_age_days=data.get("max_age_days"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        return {
            "keep_last": self.keep_last,
            "keep_daily": self.keep_daily,
            "keep_weekly": self.keep_weekly,
            "keep_monthly": self.keep_monthly,
            "max_age_days": self.max_age_days,
        }

    def has_any_policy(self) -> bool:
        """Check if any retention policy is set."""
        return any([
            self.keep_last,
            self.keep_daily,
            self.keep_weekly,
            self.keep_monthly,
            self.max_age_days,
        ])


class RetentionManager:
    """Manages retention policies and cleanup of old backups."""

    def __init__(self, storage: Storage):
        self.storage = storage

    def apply_retention(self, job_name: str, dry_run: bool = False) -> list[dict[str, Any]]:
        """Apply retention policy to a job's backup history.

        Args:
            job_name: Name of the job
            dry_run: If True, don't actually delete, just return what would be deleted

        Returns:
            List of runs that were (or would be) deleted
        """
        job = self.storage.get_job(job_name)
        if not job:
            logger.warning(f"Job not found: {job_name}")
            return []

        retention_data = job.get("retention", {})
        if not retention_data:
            return []

        policy = RetentionPolicy.from_dict(retention_data)
        if not policy.has_any_policy():
            return []

        # Get all runs for this job
        runs = self.storage.get_job_runs(job_name, limit=1000)
        if not runs:
            return []

        # Determine which runs to keep
        runs_to_keep = set()
        now = datetime.now()

        # Sort by start time (newest first)
        runs_sorted = sorted(
            runs,
            key=lambda r: r.get("started_at", ""),
            reverse=True
        )

        # Keep last N
        if policy.keep_last:
            for run in runs_sorted[:policy.keep_last]:
                runs_to_keep.add(run["run_id"])

        # Keep daily (one per day for N days)
        if policy.keep_daily:
            days_seen = set()
            for run in runs_sorted:
                started = run.get("started_at", "")[:10]  # YYYY-MM-DD
                if started and started not in days_seen and len(days_seen) < policy.keep_daily:
                    runs_to_keep.add(run["run_id"])
                    days_seen.add(started)

        # Keep weekly (one per week for N weeks)
        if policy.keep_weekly:
            weeks_seen = set()
            for run in runs_sorted:
                started = run.get("started_at")
                if started:
                    try:
                        dt = datetime.fromisoformat(started)
                        week_key = dt.strftime("%Y-W%W")
                        if week_key not in weeks_seen and len(weeks_seen) < policy.keep_weekly:
                            runs_to_keep.add(run["run_id"])
                            weeks_seen.add(week_key)
                    except ValueError:
                        pass

        # Keep monthly (one per month for N months)
        if policy.keep_monthly:
            months_seen = set()
            for run in runs_sorted:
                started = run.get("started_at")
                if started:
                    try:
                        dt = datetime.fromisoformat(started)
                        month_key = dt.strftime("%Y-%m")
                        if month_key not in months_seen and len(months_seen) < policy.keep_monthly:
                            runs_to_keep.add(run["run_id"])
                            months_seen.add(month_key)
                    except ValueError:
                        pass

        # Find runs to delete
        runs_to_delete = []
        cutoff_date = None
        if policy.max_age_days:
            cutoff_date = now - timedelta(days=policy.max_age_days)

        for run in runs:
            run_id = run["run_id"]

            # Check if marked to keep
            if run_id in runs_to_keep:
                continue

            # Check max age
            if cutoff_date:
                started = run.get("started_at")
                if started:
                    try:
                        dt = datetime.fromisoformat(started)
                        if dt < cutoff_date:
                            runs_to_delete.append(run)
                            continue
                    except ValueError:
                        pass

            # If we have keep policies, delete runs not in keep set
            if policy.keep_last or policy.keep_daily or policy.keep_weekly or policy.keep_monthly:
                runs_to_delete.append(run)

        # Delete runs (only affects database records, not actual backup files)
        if not dry_run:
            for run in runs_to_delete:
                self._delete_run(job_name, run["run_id"])
                logger.info(f"Deleted run {run['run_id']} for job {job_name}")

        return runs_to_delete

    def _delete_run(self, job_name: str, run_id: str) -> None:
        """Delete a job run record and associated progress from the database."""
        with self.storage._connect() as conn:
            conn.execute(
                "DELETE FROM job_runs WHERE job_name = ? AND run_id = ?",
                (job_name, run_id),
            )
            # Also clean up any orphaned progress records
            conn.execute(
                "DELETE FROM job_progress WHERE run_id = ?",
                (run_id,),
            )

    def apply_all_retention(self, dry_run: bool = False) -> dict[str, list[dict[str, Any]]]:
        """Apply retention policies to all jobs (regular and hypervisor).

        Returns:
            Dict mapping job names to lists of deleted runs
        """
        results = {}

        # Regular agent jobs
        jobs = self.storage.list_jobs()
        for job in jobs:
            job_name = job.get("name")
            if job_name:
                deleted = self.apply_retention(job_name, dry_run=dry_run)
                if deleted:
                    results[job_name] = deleted

        # Hypervisor backup jobs
        hv_jobs = self.storage.list_hypervisor_jobs()
        for job in hv_jobs:
            job_id = job.get("id")
            job_name = job.get("name", job_id)
            if job_id:
                deleted = self.apply_hypervisor_retention(job_id, dry_run=dry_run)
                if deleted:
                    results[f"hypervisor:{job_name}"] = deleted

        return results

    def apply_hypervisor_retention(
        self, job_id: str, dry_run: bool = False
    ) -> list[dict[str, Any]]:
        """Apply retention policy to a hypervisor job's backup history.

        This handles both database cleanup and actual file deletion from repositories.

        Args:
            job_id: ID of the hypervisor job
            dry_run: If True, don't actually delete, just return what would be deleted

        Returns:
            List of runs that were (or would be) deleted
        """
        job = self.storage.get_hypervisor_job(job_id)
        if not job:
            logger.warning(f"Hypervisor job not found: {job_id}")
            return []

        retention_data = job.get("retention", {})
        if not retention_data:
            return []

        policy = RetentionPolicy.from_dict(retention_data)
        if not policy.has_any_policy():
            return []

        # Get all runs for this job
        runs = self.storage.get_hypervisor_runs(job_id=job_id, limit=1000)
        if not runs:
            return []

        # Determine which runs to keep using the same logic as regular jobs
        runs_to_keep = self._calculate_runs_to_keep(runs, policy)

        # Find runs to delete
        runs_to_delete = []
        cutoff_date = None
        if policy.max_age_days:
            cutoff_date = datetime.now() - timedelta(days=policy.max_age_days)

        for run in runs:
            run_id = run["run_id"]

            # Check if marked to keep
            if run_id in runs_to_keep:
                continue

            # Check max age
            if cutoff_date:
                started = run.get("started_at")
                if started:
                    try:
                        dt = datetime.fromisoformat(started)
                        if dt < cutoff_date:
                            runs_to_delete.append(run)
                            continue
                    except ValueError:
                        pass

            # If we have keep policies, delete runs not in keep set
            if policy.keep_last or policy.keep_daily or policy.keep_weekly or policy.keep_monthly:
                runs_to_delete.append(run)

        if not dry_run and runs_to_delete:
            # Get repository info for file deletion
            repo_id = job.get("repository_id")
            repository = self.storage.get_repository(repo_id) if repo_id else None
            hypervisor = self.storage.get_hypervisor(job.get("hypervisor_id"))

            # Delete backup files from repository
            if repository and hypervisor:
                self._delete_hypervisor_backup_files(
                    runs_to_delete, repository, hypervisor, job
                )

            # Delete database records
            for run in runs_to_delete:
                self._delete_hypervisor_run(run)
                logger.info(
                    f"Deleted hypervisor run {run['run_id']} VM {run.get('guest_id')} "
                    f"for job {job.get('name')}"
                )

        return runs_to_delete

    def _calculate_runs_to_keep(
        self, runs: list[dict[str, Any]], policy: RetentionPolicy
    ) -> set[str]:
        """Calculate which run IDs should be kept based on retention policy."""
        runs_to_keep: set[str] = set()

        # Sort by start time (newest first)
        runs_sorted = sorted(
            runs,
            key=lambda r: r.get("started_at", ""),
            reverse=True
        )

        # Keep last N
        if policy.keep_last:
            for run in runs_sorted[:policy.keep_last]:
                runs_to_keep.add(run["run_id"])

        # Keep daily (one per day for N days)
        if policy.keep_daily:
            days_seen: set[str] = set()
            for run in runs_sorted:
                started = run.get("started_at", "")[:10]  # YYYY-MM-DD
                if started and started not in days_seen and len(days_seen) < policy.keep_daily:
                    runs_to_keep.add(run["run_id"])
                    days_seen.add(started)

        # Keep weekly (one per week for N weeks)
        if policy.keep_weekly:
            weeks_seen: set[str] = set()
            for run in runs_sorted:
                started = run.get("started_at")
                if started:
                    try:
                        dt = datetime.fromisoformat(started)
                        week_key = dt.strftime("%Y-W%W")
                        if week_key not in weeks_seen and len(weeks_seen) < policy.keep_weekly:
                            runs_to_keep.add(run["run_id"])
                            weeks_seen.add(week_key)
                    except ValueError:
                        pass

        # Keep monthly (one per month for N months)
        if policy.keep_monthly:
            months_seen: set[str] = set()
            for run in runs_sorted:
                started = run.get("started_at")
                if started:
                    try:
                        dt = datetime.fromisoformat(started)
                        month_key = dt.strftime("%Y-%m")
                        if month_key not in months_seen and len(months_seen) < policy.keep_monthly:
                            runs_to_keep.add(run["run_id"])
                            months_seen.add(month_key)
                    except ValueError:
                        pass

        return runs_to_keep

    def _delete_hypervisor_run(self, run: dict[str, Any]) -> None:
        """Delete a hypervisor run record from the database."""
        with self.storage._connect() as conn:
            conn.execute(
                "DELETE FROM hypervisor_runs WHERE run_id = ? AND guest_id = ?",
                (run["run_id"], run["guest_id"]),
            )

    def _delete_hypervisor_backup_files(
        self,
        runs: list[dict[str, Any]],
        repository: dict[str, Any],
        hypervisor: dict[str, Any],
        job: dict[str, Any],
    ) -> None:
        """Delete actual backup files from the repository.

        This handles NFS and SMB repositories by mounting them and deleting
        the vzdump files that correspond to the runs being pruned.

        IMPORTANT: All jobs from the same hypervisor share the same dump folder:
        Hypervisors/{hv_name}/dump/ - we only delete files matching the specific
        run timestamps being pruned.
        """
        repo_type = repository.get("repo_type")

        # Build list of backup files to delete based on run timestamps
        # vzdump files are named: vzdump-{type}-{vmid}-{date}_{time}.vma.{ext}
        files_to_delete = []
        for run in runs:
            started_at = run.get("started_at")
            guest_id = run.get("guest_id")
            if not started_at or not guest_id:
                continue

            try:
                dt = datetime.fromisoformat(started_at)
                # vzdump uses underscore format: 2025_12_07-15_09_27
                date_str = dt.strftime("%Y_%m_%d-%H_%M")  # Match first part of timestamp
                # Pattern to match: vzdump-*-{vmid}-{date}*.vma*
                pattern = f"vzdump-*-{guest_id}-{date_str}*.vma*"
                files_to_delete.append((guest_id, pattern, dt))
            except ValueError:
                continue

        if not files_to_delete:
            return

        # Get repository path info
        hv_name = hypervisor.get("name", "unknown")

        if repo_type == "nfs":
            # NFS: Proxmox mounts the export root, vzdump creates dump/ there
            # So backups are at: {export}/dump/vzdump-*.vma.zst
            dump_path = "dump"
            self._delete_files_from_nfs(
                repository.get("server"),
                repository.get("share"),
                dump_path,
                files_to_delete,
            )
        elif repo_type == "smb":
            # SMB: Proxmox mounts at {share}/{subdir}/Hypervisors/{hv_name}
            # and vzdump creates dump/ inside that
            base_path = repository.get("path", "").strip("/")
            hv_dump_path = f"Hypervisors/{hv_name}/dump"
            if base_path:
                dump_path = f"{base_path}/{hv_dump_path}"
            else:
                dump_path = hv_dump_path

            # Get password for SMB
            repo_password = None
            if repository.get("password_encrypted"):
                from backer.server.secrets import get_secrets_manager
                secrets = get_secrets_manager(self.storage.db_path.parent)
                repo_password = secrets.decrypt(repository["password_encrypted"])

            self._delete_files_from_smb(
                repository.get("server"),
                repository.get("share"),
                repository.get("username"),
                repo_password,
                repository.get("domain"),
                dump_path,
                files_to_delete,
            )

    def _delete_files_from_nfs(
        self,
        server: str,
        export: str,
        dump_path: str,
        files_to_delete: list[tuple[int, str, datetime]],
    ) -> None:
        """Delete backup files from an NFS share."""
        if not server or not export:
            logger.warning("NFS server/export not configured")
            return

        mount_point = None
        try:
            # Create temporary mount point
            mount_point = tempfile.mkdtemp(prefix="backer_retention_")
            nfs_source = f"{server}:{export}"

            # Mount NFS
            mount_cmd = ["sudo", "-n", "mount", "-t", "nfs", "-o", "rw", nfs_source, mount_point]
            result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                logger.warning(f"Failed to mount NFS for retention cleanup: {result.stderr}")
                return

            # Find and delete files
            full_dump_path = Path(mount_point) / dump_path
            if not full_dump_path.exists():
                logger.debug(f"Dump path does not exist: {full_dump_path}")
                return

            deleted_count = 0
            for guest_id, pattern, timestamp in files_to_delete:
                # List matching files
                for entry in full_dump_path.iterdir():
                    if not entry.is_file():
                        continue

                    # Check if file matches our pattern (VMID and approximate time)
                    match = re.match(
                        rf"vzdump-(qemu|lxc)-{guest_id}-(\d{{4}}_\d{{2}}_\d{{2}})-"
                        rf"(\d{{2}}_\d{{2}}_\d{{2}})\.vma(?:\.(zst|gz|lzo))?$",
                        entry.name
                    )
                    if match:
                        # Parse file timestamp
                        file_date = match.group(2)  # YYYY_MM_DD
                        file_time = match.group(3)  # HH_MM_SS
                        try:
                            file_dt = datetime.strptime(
                                f"{file_date}_{file_time}", "%Y_%m_%d_%H_%M_%S"
                            )
                            # Compare by DATE only - Proxmox uses local time, we may have UTC
                            # This avoids timezone conversion issues
                            run_date = timestamp.replace(tzinfo=None).date()
                            file_date_obj = file_dt.date()

                            # Match if same date (handles timezone differences)
                            if file_date_obj == run_date:
                                try:
                                    entry.unlink()
                                    deleted_count += 1
                                    logger.info(f"Deleted backup file: {entry.name}")
                                    # Also delete .notes and .log files if exists
                                    notes_file = Path(str(entry) + ".notes")
                                    if notes_file.exists():
                                        notes_file.unlink()
                                    log_file = entry.with_suffix(".log")
                                    if log_file.exists():
                                        log_file.unlink()
                                except OSError as e:
                                    logger.warning(f"Failed to delete {entry.name}: {e}")
                        except ValueError:
                            continue

            if deleted_count > 0:
                logger.info(f"Retention cleanup deleted {deleted_count} backup files from NFS")

        except subprocess.TimeoutExpired:
            logger.warning("NFS mount timeout during retention cleanup")
        except Exception as e:
            logger.warning(f"Error during NFS retention cleanup: {e}")
        finally:
            # Unmount
            if mount_point:
                try:
                    subprocess.run(
                        ["sudo", "-n", "umount", mount_point],
                        capture_output=True, timeout=30
                    )
                    os.rmdir(mount_point)
                except Exception:
                    pass

    def _delete_files_from_smb(
        self,
        server: str,
        share: str,
        username: str | None,
        password: str | None,
        domain: str | None,
        dump_path: str,
        files_to_delete: list[tuple[int, str, datetime]],
    ) -> None:
        """Delete backup files from an SMB share."""
        if not server or not share:
            logger.warning("SMB server/share not configured")
            return

        try:
            # Build smbclient auth
            auth_opts = ["-N"]  # No password prompt
            if username and password:
                auth_opts = ["-U", f"{username}%{password}"]
                if domain:
                    auth_opts.extend(["-W", domain])

            # First, list files in the dump directory
            list_cmd = ["smbclient", f"//{server}/{share}", *auth_opts, "-c", f"cd {dump_path}; ls"]
            result = subprocess.run(list_cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                logger.debug(f"Could not list SMB dump path: {result.stderr}")
                return

            # Parse file list and find matches
            deleted_count = 0
            for guest_id, pattern, timestamp in files_to_delete:
                for line in result.stdout.split("\n"):
                    # smbclient ls output format: "  filename  ATTR  SIZE  DATE"
                    match = re.search(
                        rf"(vzdump-(qemu|lxc)-{guest_id}-(\d{{4}}_\d{{2}}_\d{{2}})-"
                        rf"(\d{{2}}_\d{{2}}_\d{{2}})\.vma(?:\.(zst|gz|lzo))?)",
                        line
                    )
                    if match:
                        filename = match.group(1)
                        file_date = match.group(3)  # YYYY_MM_DD
                        file_time = match.group(4)  # HH_MM_SS
                        try:
                            file_dt = datetime.strptime(
                                f"{file_date}_{file_time}", "%Y_%m_%d_%H_%M_%S"
                            )
                            # Compare by DATE only - Proxmox uses local time, we may have UTC
                            run_date = timestamp.replace(tzinfo=None).date()
                            file_date_obj = file_dt.date()

                            if file_date_obj == run_date:
                                # Delete file via smbclient
                                del_cmd = [
                                    "smbclient", f"//{server}/{share}", *auth_opts,
                                    "-c", f"cd {dump_path}; rm \"{filename}\""
                                ]
                                del_result = subprocess.run(
                                    del_cmd, capture_output=True, text=True, timeout=30
                                )
                                if del_result.returncode == 0:
                                    deleted_count += 1
                                    logger.info(f"Deleted backup file: {filename}")
                                else:
                                    logger.warning(
                                        f"Failed to delete {filename}: {del_result.stderr}"
                                    )
                        except ValueError:
                            continue

            if deleted_count > 0:
                logger.info(f"Retention cleanup deleted {deleted_count} backup files from SMB")

        except subprocess.TimeoutExpired:
            logger.warning("SMB command timeout during retention cleanup")
        except Exception as e:
            logger.warning(f"Error during SMB retention cleanup: {e}")


# Preset retention policies
RETENTION_PRESETS = {
    "minimal": {
        "keep_last": 3,
        "description": "Keep last 3 backups",
    },
    "standard": {
        "keep_last": 7,
        "keep_daily": 7,
        "keep_weekly": 4,
        "description": "7 daily + 4 weekly",
    },
    "extended": {
        "keep_last": 14,
        "keep_daily": 14,
        "keep_weekly": 8,
        "keep_monthly": 12,
        "description": "14 daily + 8 weekly + 12 monthly",
    },
    "forever": {
        "description": "Keep all backups",
    },
}
