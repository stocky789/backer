"""Retention policy management for backups."""

import logging
from datetime import datetime, timedelta
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
        """Apply retention policies to all jobs.

        Returns:
            Dict mapping job names to lists of deleted runs
        """
        jobs = self.storage.list_jobs()
        results = {}

        for job in jobs:
            job_name = job.get("name")
            if job_name:
                deleted = self.apply_retention(job_name, dry_run=dry_run)
                if deleted:
                    results[job_name] = deleted

        return results


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
