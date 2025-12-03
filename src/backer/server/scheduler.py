"""Backup job scheduler - runs jobs according to their cron schedules."""

import logging
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

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
        check_interval: int = 60,
    ):
        """Initialize the scheduler.

        Args:
            storage: Storage instance to read job configs
            job_trigger_callback: Function to call when a job should run.
                                  Takes job_name as argument.
            check_interval: How often to check for due jobs (seconds)
        """
        self.storage = storage
        self.job_trigger_callback = job_trigger_callback
        self.check_interval = check_interval

        self._running = False
        self._thread: threading.Thread | None = None
        self._last_run_times: dict[str, datetime] = {}

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
            except Exception as e:
                logger.error(f"Scheduler error: {e}")

            # Sleep in small increments for responsive shutdown
            for _ in range(self.check_interval):
                if not self._running:
                    break
                threading.Event().wait(1)

    def _check_and_run_jobs(self) -> None:
        """Check all jobs and run any that are due."""
        jobs = self.storage.list_jobs()
        now = datetime.now()

        for job in jobs:
            job_name = job.get("name")
            schedule = job.get("schedule_cron")
            enabled = job.get("enabled", True)

            if not schedule or not enabled:
                continue

            try:
                if self._is_job_due(job_name, schedule, now):
                    logger.info(f"Triggering scheduled job: {job_name}")
                    self._trigger_job(job_name)
                    self._last_run_times[job_name] = now
            except Exception as e:
                logger.error(f"Error checking job {job_name}: {e}")

    def _is_job_due(self, job_name: str, schedule: str, now: datetime) -> bool:
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
            last_run = self._last_run_times.get(job_name)
            if last_run:
                since_last = (now - last_run).total_seconds()
                if since_last < 60:
                    return False

            return is_due

        except Exception as e:
            logger.error(f"Invalid cron expression for {job_name}: {schedule} - {e}")
            return False

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
            from_time: Calculate next run from this time (default: now)

        Returns:
            Next run datetime, or None if invalid schedule
        """
        if not HAS_CRONITER or not schedule:
            return None

        try:
            base_time = from_time or datetime.now()
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
        now = datetime.now()

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
