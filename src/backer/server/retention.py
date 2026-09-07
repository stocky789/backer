"""Retention policy management for backups."""

import logging
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from backer.server import timezone as tz
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
        for name, value in {
            "keep_last": keep_last, "keep_daily": keep_daily, "keep_weekly": keep_weekly,
            "keep_monthly": keep_monthly, "max_age_days": max_age_days,
        }.items():
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ValueError(f"{name} must be a non-negative integer")
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
        return any(value is not None for value in (
            self.keep_last, self.keep_daily, self.keep_weekly, self.keep_monthly, self.max_age_days,
        ))


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
        now = tz.get_now()

        # Sort by start time (newest first)
        runs_sorted = sorted(
            runs,
            key=lambda r: r.get("started_at", ""),
            reverse=True
        )
        # A valid retention policy must not turn a transient clock/config error
        # into deletion of the only recoverable generation.
        if runs_sorted:
            runs_to_keep.add(runs_sorted[0]["run_id"])

        # Keep last N
        if policy.keep_last is not None:
            for run in runs_sorted[:policy.keep_last]:
                runs_to_keep.add(run["run_id"])

        # Keep daily (one per day for N days)
        if policy.keep_daily is not None:
            days_seen = set()
            for run in runs_sorted:
                started = run.get("started_at", "")[:10]  # YYYY-MM-DD
                if started and started not in days_seen and len(days_seen) < policy.keep_daily:
                    runs_to_keep.add(run["run_id"])
                    days_seen.add(started)

        # Keep weekly (one per week for N weeks)
        if policy.keep_weekly is not None:
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
        if policy.keep_monthly is not None:
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
        if policy.max_age_days is not None:
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
            if any(value is not None for value in (
                policy.keep_last, policy.keep_daily, policy.keep_weekly, policy.keep_monthly,
            )):
                runs_to_delete.append(run)

        # Managed repository history is only an index. Never remove it until
        # the exact corresponding physical snapshots were pruned successfully.
        if runs_to_delete and not job.get("repository_id"):
            raise RuntimeError("Managed retention refuses jobs without a repository")
        if runs_to_delete and job.get("repository_id"):
            repo = self.storage.get_repository(job["repository_id"])
            self._prune_managed_snapshots(job, repo, runs, runs_to_delete, policy, dry_run)
        if not dry_run:
            for run in runs_to_delete:
                self._delete_run(job_name, run["run_id"])
                logger.info(f"Deleted run {run['run_id']} for job {job_name}")

        return runs_to_delete

    @staticmethod
    def _snapshot_ids(runs: list[dict[str, Any]]) -> set[str]:
        """Return the durable IDs, refusing ambiguous history before deletion."""
        ids = [run.get("snapshot_id") or run.get("run_id") for run in runs]
        if any(not isinstance(snapshot_id, str) or not snapshot_id for snapshot_id in ids) or len(set(ids)) != len(ids):
            raise RuntimeError("Managed retention refuses runs without unique snapshot IDs")
        return set(ids)

    @contextmanager
    def _repository_operation_path(self, repo: dict[str, Any]):
        """Yield a local/UNC repository path using the normal SMB mount flow."""
        repo_type = repo.get("repo_type")
        if repo_type == "local":
            share = repo.get("share")
            if not isinstance(share, str) or not share:
                raise RuntimeError("Managed retention refuses a repository without a local path")
            yield Path(share)
            return
        if repo_type != "smb":
            raise RuntimeError("Managed retention refuses unsupported repository storage")

        from backer.core.config import RepositoryConfig
        from backer.serverless.repositories import _destination, repository_operation_context

        try:
            record = RepositoryConfig(
                id=repo.get("id"), name=repo.get("name") or repo.get("id") or "repository",
                format=repo.get("format", "kopia"), type="smb", path=repo.get("path"),
                server=repo.get("server"), share=repo.get("share"), username=repo.get("username"),
                domain=repo.get("domain"),
            )
        except ValueError as error:
            raise RuntimeError(f"Managed retention refuses invalid SMB repository: {error}") from error
        password = self.storage.get_storage_password(repo["id"])
        with repository_operation_context(record, password) as operation_repo:
            yield Path(_destination(operation_repo))

    def _prune_managed_snapshots(
        self,
        job: dict[str, Any],
        repo: dict[str, Any] | None,
        all_runs: list[dict[str, Any]],
        runs_to_delete: list[dict[str, Any]],
        policy: RetentionPolicy,
        dry_run: bool,
    ) -> None:
        """Preview, match, then prune; DB cleanup is deliberately last."""
        if not repo:
            raise RuntimeError("Managed retention refuses an unknown repository")
        if policy.max_age_days is not None:
            raise RuntimeError("Managed retention does not support max_age_days for physical snapshots")
        expected = self._snapshot_ids(runs_to_delete)
        all_ids = self._snapshot_ids(all_runs)
        repository_format = repo.get("format", "kopia")
        if repository_format not in {"files", "kopia"}:
            raise RuntimeError("Managed retention refuses an unsupported repository format")

        from backer.backends.base import BackupDestination
        from backer.core.paths import get_job_subfolder

        with self._repository_operation_path(repo) as repository_path:
            if repository_format == "files":
                from backer.backends.registry import get_backend

                destination = BackupDestination(str(repository_path / "Agents" / get_job_subfolder(job["name"])))
                backend = get_backend("files", {"repository_id": repo["id"], "job_name": job["name"]})
                physical_ids = {row.get("full_id") for row in backend.list_snapshots(destination)}
                if physical_ids != all_ids:
                    raise RuntimeError("Managed retention refused files history that does not exactly match snapshots")
                preview = backend.prune(
                    destination, keep_last=policy.keep_last, keep_daily=policy.keep_daily,
                    keep_weekly=policy.keep_weekly, keep_monthly=policy.keep_monthly, dry_run=True,
                )
                preview_ids = set(preview.metadata.get("deleted_snapshot_ids", []))
                if not preview.success or preview_ids != expected:
                    raise RuntimeError(
                        "Managed retention refused DB cleanup because physical snapshot candidates did not match"
                    )
                if not dry_run:
                    applied = backend.prune(
                        destination, keep_last=policy.keep_last, keep_daily=policy.keep_daily,
                        keep_weekly=policy.keep_weekly, keep_monthly=policy.keep_monthly, dry_run=False,
                    )
                    if not applied.success or set(applied.metadata.get("deleted_snapshot_ids", [])) != expected:
                        raise RuntimeError("Managed retention refused DB cleanup because files deletion was incomplete")
                return

            if repo.get("repo_type") == "local":
                self._prune_proxy_kopia(job["name"], repository_path, repo, all_ids, expected, dry_run)
            else:
                self._prune_direct_kopia(job, repository_path, repo, all_ids, expected, policy, dry_run)

    def _prune_proxy_kopia(
        self, job_name: str, repository_path: Path, repo: dict[str, Any], all_ids: set[str], expected: set[str],
        dry_run: bool,
    ) -> None:
        """Delete only listed, job-tagged proxy snapshots by their immutable IDs."""
        from backer.server.app import ServerKopia

        password = self.storage.get_repository_password(repo["id"])
        if not password:
            raise RuntimeError("Managed retention refuses a Kopia repository without its password")
        kopia = ServerKopia(str(repository_path), password)
        if not kopia.ensure_repo(create_if_absent=False):
            raise RuntimeError("Managed retention could not connect to the Kopia repository")
        snapshots = kopia.snapshot_list(job_name)
        physical_ids = {row.get("full_id") for row in snapshots}
        if physical_ids != all_ids or not expected <= physical_ids:
            raise RuntimeError("Managed retention refused Kopia history that does not exactly match snapshots")
        if dry_run:
            return
        # Each ID is passed as a separate positional argument; no source/path
        # selector can widen this deletion to another job.
        result = kopia.maintenance(["snapshot", "delete", "--delete", *sorted(expected)])
        if not result.get("success"):
            raise RuntimeError(
                f"Managed retention failed to delete Kopia snapshots: {result.get('error') or 'unknown error'}"
            )
        survivors = {row.get("full_id") for row in kopia.snapshot_list(job_name)}
        if survivors != all_ids - expected:
            raise RuntimeError("Managed retention refused DB cleanup because Kopia survivors did not match")

    def _prune_direct_kopia(
        self, job: dict[str, Any], repository_path: Path, repo: dict[str, Any], all_ids: set[str], expected: set[str],
        policy: RetentionPolicy, dry_run: bool,
    ) -> None:
        """Use Kopia's source-scoped retention flow for direct SMB repositories."""
        from backer.backends.base import BackupDestination
        from backer.backends.registry import get_backend

        password = self.storage.get_repository_password(repo["id"])
        if not password or not isinstance(job.get("source_path"), str) or not job["source_path"]:
            raise RuntimeError("Managed retention refuses an unscoped Kopia repository")
        backend = get_backend("kopia", {"repository_password": password})
        destination = BackupDestination(str(repository_path))
        snapshots = backend.list_snapshots(destination)
        source = os.path.normcase(os.path.normpath(job["source_path"]))
        physical_ids = {
            row.get("full_id") for row in snapshots
            if any(os.path.normcase(os.path.normpath(str(path))) == source for path in row.get("paths", []))
        }
        if physical_ids != all_ids:
            raise RuntimeError("Managed retention refused Kopia history that does not exactly match snapshots")
        preview = backend.prune(
            destination, keep_last=policy.keep_last, keep_daily=policy.keep_daily, keep_weekly=policy.keep_weekly,
            keep_monthly=policy.keep_monthly, dry_run=True, source_path=job["source_path"], list_expired=True,
        )
        preview_ids = {row.get("id") for row in preview.metadata.get("expired_snapshots", [])}
        if not preview.success or preview_ids != expected:
            raise RuntimeError(
                "Managed retention refused DB cleanup because physical snapshot candidates did not match"
            )
        if not dry_run:
            applied = backend.prune(
                destination, keep_last=policy.keep_last, keep_daily=policy.keep_daily, keep_weekly=policy.keep_weekly,
                keep_monthly=policy.keep_monthly, dry_run=False, source_path=job["source_path"],
            )
            if not applied.success:
                raise RuntimeError("Managed retention refused DB cleanup because Kopia deletion failed")
            remaining = {
                row.get("full_id") for row in backend.list_snapshots(destination)
                if any(os.path.normcase(os.path.normpath(str(path))) == source for path in row.get("paths", []))
            }
            if remaining != all_ids - expected:
                raise RuntimeError("Managed retention refused DB cleanup because Kopia survivors did not match")

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
        if policy.max_age_days is not None:
            cutoff_date = tz.get_now() - timedelta(days=policy.max_age_days)

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
                        # Ensure timezone-aware for comparison
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=UTC)
                        if dt < cutoff_date:
                            runs_to_delete.append(run)
                            continue
                    except ValueError:
                        pass

            # If we have keep policies, delete runs not in keep set
            if any(value is not None for value in (
                policy.keep_last, policy.keep_daily, policy.keep_weekly, policy.keep_monthly,
            )):
                runs_to_delete.append(run)

        if not dry_run and runs_to_delete:
            # Get repository info for file deletion
            repo_id = job.get("repository_id")
            repository = self.storage.get_repository(repo_id) if repo_id else None
            hypervisor = self.storage.get_hypervisor(job.get("hypervisor_id"))

            if not repository or not hypervisor:
                raise RuntimeError("Hypervisor retention refuses DB cleanup without repository storage")
            if not self._delete_hypervisor_backup_files(runs_to_delete, repository, hypervisor, job):
                raise RuntimeError("Hypervisor retention refused DB cleanup because physical deletion was incomplete")

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
        if runs_sorted:
            runs_to_keep.add(runs_sorted[0]["run_id"])

        # Keep last N
        if policy.keep_last is not None:
            for run in runs_sorted[:policy.keep_last]:
                runs_to_keep.add(run["run_id"])

        # Keep daily (one per day for N days)
        if policy.keep_daily is not None:
            days_seen: set[str] = set()
            for run in runs_sorted:
                started = run.get("started_at", "")[:10]  # YYYY-MM-DD
                if started and started not in days_seen and len(days_seen) < policy.keep_daily:
                    runs_to_keep.add(run["run_id"])
                    days_seen.add(started)

        # Keep weekly (one per week for N weeks)
        if policy.keep_weekly is not None:
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
        if policy.keep_monthly is not None:
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
    ) -> bool:
        """Delete actual backup files from the repository.

        This handles NFS and SMB repositories by mounting them and deleting
        the vzdump files that correspond to the runs being pruned.

        IMPORTANT: All jobs from the same hypervisor share the same dump folder:
        Hypervisors/{hv_name}/dump/ - we only delete files matching the specific
        run timestamps being pruned.
        """
        repo_type = repository.get("repo_type")

        # Build exact primary backup filenames. Matching by VM/date is unsafe:
        # several generations for one VM can exist on the same day.
        # vzdump files are named: vzdump-{type}-{vmid}-{date}_{time}.vma.{ext}
        files_to_delete = []
        for run in runs:
            started_at = run.get("started_at")
            guest_id = run.get("guest_id")
            if not started_at or not guest_id:
                continue

            try:
                dt = datetime.fromisoformat(started_at)
                files_to_delete.append((str(guest_id), dt.strftime("%Y_%m_%d-%H_%M_%S")))
            except ValueError:
                return False

        if len(files_to_delete) != len(runs) or len(set(files_to_delete)) != len(files_to_delete):
            return False

        # Get repository path info
        hv_name = hypervisor.get("name", "unknown")

        if repo_type == "nfs":
            # NFS: Proxmox mounts the export root, vzdump creates dump/ there
            # So backups are at: {export}/dump/vzdump-*.vma.zst
            dump_path = "dump"
            return self._delete_files_from_nfs(
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

            repo_password = self.storage.get_storage_password(repository["id"])

            return self._delete_files_from_smb(
                repository.get("server"),
                repository.get("share"),
                repository.get("username"),
                repo_password,
                repository.get("domain"),
                dump_path,
                files_to_delete,
            )
        return False

    def _delete_files_from_nfs(
        self,
        server: str,
        export: str,
        dump_path: str,
        files_to_delete: list[tuple[str, str]],
    ) -> bool:
        """Delete backup files from an NFS share."""
        if not server or not export:
            logger.warning("NFS server/export not configured")
            return False

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
                return False

            # Find and delete files
            full_dump_path = Path(mount_point) / dump_path
            if not full_dump_path.exists():
                logger.debug(f"Dump path does not exist: {full_dump_path}")
                return False
            targets = {
                f"vzdump-{kind}-{guest_id}-{stamp}.vma{suffix}"
                for guest_id, stamp in files_to_delete
                for kind in ("qemu", "lxc")
                for suffix in ("", ".zst", ".gz", ".lzo")
            }
            matches = [entry for entry in full_dump_path.iterdir() if entry.is_file() and entry.name in targets]
            if len(matches) != len(files_to_delete):
                return False
            for entry in matches:
                entry.unlink()
                Path(f"{entry}.notes").unlink(missing_ok=True)
                base = entry.name.split(".vma", 1)[0]
                (entry.parent / f"{base}.log").unlink(missing_ok=True)
            return True

        except subprocess.TimeoutExpired:
            logger.warning("NFS mount timeout during retention cleanup")
            return False
        except Exception as e:
            logger.warning(f"Error during NFS retention cleanup: {e}")
            return False
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
        files_to_delete: list[tuple[str, str]],
    ) -> bool:
        """Delete backup files from an SMB share."""
        if not server or not share:
            logger.warning("SMB server/share not configured")
            return False

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
                return False

            wanted = {
                f"vzdump-{kind}-{guest_id}-{stamp}.vma{suffix}"
                for guest_id, stamp in files_to_delete
                for kind in ("qemu", "lxc")
                for suffix in ("", ".zst", ".gz", ".lzo")
            }
            names = {
                match.group(1)
                for line in result.stdout.splitlines()
                if (match := re.search(r"(vzdump-(?:qemu|lxc)-[^\s]+\.vma(?:\.(?:zst|gz|lzo))?)", line))
            }
            matches = names & wanted
            if len(matches) != len(files_to_delete):
                return False
            for filename in matches:
                command = f'cd {dump_path}; rm "{filename}"'
                deleted = subprocess.run(
                    ["smbclient", f"//{server}/{share}", *auth_opts, "-c", command],
                    capture_output=True, text=True, timeout=30,
                )
                if deleted.returncode != 0:
                    return False
            return True

        except subprocess.TimeoutExpired:
            logger.warning("SMB command timeout during retention cleanup")
            return False
        except Exception as e:
            logger.warning(f"Error during SMB retention cleanup: {e}")
            return False


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
