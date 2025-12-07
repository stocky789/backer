"""Backup job scheduler - runs jobs according to their cron schedules."""

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

try:
    from croniter import croniter
    HAS_CRONITER = True
except ImportError:
    HAS_CRONITER = False

if TYPE_CHECKING:
    from backer.server.storage import Storage

logger = logging.getLogger(__name__)


class BackupScheduler:
    """Scheduler that runs backup jobs according to their cron schedules.

    The scheduler runs in a background thread and checks every minute
    for jobs that need to be executed.
    """

    def __init__(
        self,
        storage: "Storage",
        job_trigger_callback: Callable[[str], Any],
        hypervisor_job_trigger_callback: Callable[[str], Any] | None = None,
        check_interval: int = 60,
    ):
        """Initialize the scheduler.

        Args:
            storage: Storage instance to read job configs
            job_trigger_callback: Function to call when a job should run.
                                  Takes job_name as argument.
            hypervisor_job_trigger_callback: Function to call when a hypervisor
                                             backup job should run. Takes job_id.
            check_interval: How often to check for due jobs (seconds)
        """
        self.storage = storage
        self.job_trigger_callback = job_trigger_callback
        self.hypervisor_job_trigger_callback = hypervisor_job_trigger_callback
        self.check_interval = check_interval

        self._running = False
        self._thread: threading.Thread | None = None
        self._last_run_times: dict[str, datetime] = {}
        self._last_hypervisor_run_times: dict[str, datetime] = {}

        # Timezone caching to avoid database hits on every _now() call
        self._cached_timezone: ZoneInfo | None = None
        self._timezone_cache_time: datetime | None = None
        self._timezone_cache_ttl = 300  # Refresh timezone cache every 5 minutes

        # Cleanup tracking
        self._last_cleanup: datetime | None = None
        self._cleanup_interval = 300  # Run cleanup every 5 minutes

    def _get_timezone(self) -> ZoneInfo:
        """Get the configured timezone from storage with caching.

        Returns ZoneInfo for the configured timezone, or UTC if not set or invalid.
        Uses a cache to avoid hitting the database on every call.
        """
        now = datetime.now()

        # Check if cache is valid
        if (
            self._cached_timezone is not None
            and self._timezone_cache_time is not None
            and (now - self._timezone_cache_time).total_seconds() < self._timezone_cache_ttl
        ):
            return self._cached_timezone

        # Cache miss or expired - fetch from storage
        tz_name = self.storage.get_setting("timezone", "UTC")
        try:
            tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
        except Exception:
            logger.warning(f"Invalid timezone '{tz_name}', using UTC")
            tz = ZoneInfo("UTC")

        # Update cache
        self._cached_timezone = tz
        self._timezone_cache_time = now

        return tz

    def _now(self) -> datetime:
        """Get current time in the configured timezone."""
        tz = self._get_timezone()
        return datetime.now(tz)

    def start(self) -> None:
        """Start the scheduler in a background thread."""
        if not HAS_CRONITER:
            logger.warning("croniter not installed - scheduler disabled. Install with: pip install croniter")
            return

        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Backup scheduler started")

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Backup scheduler stopped")

    def _run_loop(self) -> None:
        """Main scheduler loop."""
        while self._running:
            try:
                self._check_and_run_jobs()
                self._run_cleanup_if_due()
            except Exception as e:
                logger.error(f"Scheduler error: {e}")

            # Sleep in small increments for responsive shutdown
            for _ in range(self.check_interval):
                if not self._running:
                    break
                threading.Event().wait(1)

    def _run_cleanup_if_due(self) -> None:
        """Run periodic cleanup tasks if enough time has passed."""
        now = datetime.now()

        # Check if cleanup is due
        if self._last_cleanup is not None:
            since_last = (now - self._last_cleanup).total_seconds()
            if since_last < self._cleanup_interval:
                return

        try:
            # Clean up stale browse requests (older than 10 minutes)
            browse_cleaned = self.storage.cleanup_old_browse_requests(max_age_minutes=10)
            if browse_cleaned > 0:
                logger.debug(f"Cleaned up {browse_cleaned} stale browse requests")

            # Clean up stale progress records (older than 60 minutes)
            progress_cleaned = self.storage.cleanup_stale_progress(max_age_minutes=60)
            if progress_cleaned > 0:
                logger.debug(f"Cleaned up {progress_cleaned} stale progress records")

            # Apply retention policies to all jobs (every cleanup cycle)
            self._apply_retention_policies()

            self._last_cleanup = now

        except Exception as e:
            logger.warning(f"Cleanup error: {e}")

    def _apply_retention_policies(self) -> None:
        """Apply retention policies to all jobs, deleting old backup runs."""
        try:
            from backer.server.retention import RetentionManager

            manager = RetentionManager(self.storage)
            results = manager.apply_all_retention(dry_run=False)

            if results:
                total_deleted = sum(len(runs) for runs in results.values())
                logger.info(
                    f"Retention cleanup: deleted {total_deleted} old runs "
                    f"across {len(results)} jobs"
                )
        except Exception as e:
            logger.warning(f"Retention policy error: {e}")

    def _check_and_run_jobs(self) -> None:
        """Check all jobs and run any that are due."""
        now = self._now()

        # Check regular backup jobs
        jobs = self.storage.list_jobs()
        for job in jobs:
            job_name = job.get("name")
            schedule = job.get("schedule_cron")
            enabled = job.get("enabled", True)

            if not schedule or not enabled:
                continue

            try:
                if self._is_job_due(job_name, schedule, now, self._last_run_times):
                    logger.info(f"Triggering scheduled job: {job_name}")
                    self._trigger_job(job_name)
                    self._last_run_times[job_name] = now
            except Exception as e:
                logger.error(f"Error checking job {job_name}: {e}")

        # Check hypervisor backup jobs
        if self.hypervisor_job_trigger_callback:
            self._check_and_run_hypervisor_jobs(now)

    def _is_job_due(
        self,
        job_id: str,
        schedule: str,
        now: datetime,
        last_run_times: dict[str, datetime],
    ) -> bool:
        """Check if a job is due to run.

        A job is due if the current time matches the cron schedule
        and the job hasn't been run in the current minute.
        """
        try:
            cron = croniter(schedule, now - timedelta(minutes=1))
            next_run = cron.get_next(datetime)

            # Check if next run time is within the current minute
            time_diff = (next_run - now).total_seconds()
            is_due = -60 <= time_diff <= 60

            # Don't run if we already ran this job in the current minute
            last_run = last_run_times.get(job_id)
            if last_run:
                since_last = (now - last_run).total_seconds()
                if since_last < 60:
                    return False

            return is_due

        except Exception as e:
            logger.error(f"Invalid cron expression for {job_id}: {schedule} - {e}")
            return False

    def _check_and_run_hypervisor_jobs(self, now: datetime) -> None:
        """Check hypervisor backup jobs and run any that are due."""
        try:
            jobs = self.storage.list_hypervisor_jobs()
        except Exception as e:
            logger.error(f"Failed to list hypervisor jobs: {e}")
            return

        for job in jobs:
            job_id = job.get("id")
            schedule = job.get("schedule_cron")
            enabled = job.get("enabled", True)

            if not schedule or not enabled:
                continue

            try:
                if self._is_job_due(job_id, schedule, now, self._last_hypervisor_run_times):
                    logger.info(f"Triggering scheduled hypervisor job: {job.get('name')} ({job_id})")
                    self._trigger_hypervisor_job(job_id)
                    self._last_hypervisor_run_times[job_id] = now
            except Exception as e:
                logger.error(f"Error checking hypervisor job {job_id}: {e}")

    def _trigger_hypervisor_job(self, job_id: str) -> None:
        """Trigger a hypervisor backup job to run."""
        if not self.hypervisor_job_trigger_callback:
            return

        try:
            self.hypervisor_job_trigger_callback(job_id)
        except Exception as e:
            logger.error(f"Failed to trigger hypervisor job {job_id}: {e}")

    def _trigger_job(self, job_name: str) -> None:
        """Trigger a job to run."""
        try:
            self.job_trigger_callback(job_name)
        except Exception as e:
            logger.error(f"Failed to trigger job {job_name}: {e}")

    def get_next_run(self, schedule: str, from_time: datetime | None = None) -> datetime | None:
        """Get the next run time for a cron schedule.

        Args:
            schedule: Cron expression
            from_time: Calculate next run from this time (default: now in configured timezone)

        Returns:
            Next run datetime, or None if invalid schedule
        """
        if not HAS_CRONITER or not schedule:
            return None

        try:
            base_time = from_time or self._now()
            cron = croniter(schedule, base_time)
            return cron.get_next(datetime)
        except Exception:
            return None

    def get_job_status(self) -> list[dict[str, Any]]:
        """Get status of all scheduled jobs.

        Returns list of dicts with job_name, schedule, next_run, last_run
        """
        jobs = self.storage.list_jobs()
        status = []
        now = self._now()

        for job in jobs:
            job_name = job.get("name")
            schedule = job.get("schedule_cron")
            enabled = job.get("enabled", True)

            job_status = {
                "job_name": job_name,
                "schedule": schedule,
                "enabled": enabled,
                "next_run": None,
                "last_scheduled_run": self._last_run_times.get(job_name),
            }

            if schedule and enabled:
                job_status["next_run"] = self.get_next_run(schedule, now)

            status.append(job_status)

        return status


def format_cron_human(cron_expr: str) -> str:
    """Convert a cron expression to human-readable format.

    Examples:
        "0 2 * * *" -> "Daily at 2:00 AM"
        "0 3 * * 0" -> "Weekly on Sunday at 3:00 AM"
        "0 */6 * * *" -> "Every 6 hours"
    """
    if not cron_expr:
        return "Manual only"

    # Common presets
    presets = {
        "0 2 * * *": "Daily at 2:00 AM",
        "0 3 * * 0": "Weekly on Sunday at 3:00 AM",
        "0 1 * * 1-5": "Weekdays at 1:00 AM",
        "0 */6 * * *": "Every 6 hours",
        "0 */12 * * *": "Every 12 hours",
        "0 0 * * *": "Daily at midnight",
        "0 0 1 * *": "Monthly on the 1st",
    }

    if cron_expr in presets:
        return presets[cron_expr]

    # Parse and describe
    try:
        parts = cron_expr.split()
        if len(parts) != 5:
            return cron_expr

        minute, hour, day, month, weekday = parts

        # Every X hours
        if hour.startswith("*/") and minute == "0" and day == "*" and month == "*" and weekday == "*":
            interval = hour[2:]
            return f"Every {interval} hours"

        # Daily at specific time
        if day == "*" and month == "*" and weekday == "*":
            if minute.isdigit() and hour.isdigit():
                h = int(hour)
                m = int(minute)
                am_pm = "AM" if h < 12 else "PM"
                h12 = h if h <= 12 else h - 12
                h12 = 12 if h12 == 0 else h12
                time_str = f"{h12}:{m:02d} {am_pm}"
                return f"Daily at {time_str}"

        return cron_expr
    except Exception:
        return cron_expr
