"""Job management and execution."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from backer.backends import get_backend
from backer.backends.base import BackendResult, BackupDestination, BackupSource
from backer.core.config import JobConfig, get_state_dir


class JobStatus(str, Enum):
    """Status of a backup job."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class JobRun:
    """Record of a single job execution."""

    job_name: str
    run_id: str
    status: JobStatus
    started_at: datetime
    finished_at: datetime | None = None
    result: BackendResult | None = None
    error_message: str | None = None
    client_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "job_name": self.job_name,
            "run_id": self.run_id,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error_message": self.error_message,
            "client_id": self.client_id,
            "result": {
                "success": self.result.success,
                "bytes_transferred": self.result.bytes_transferred,
                "files_transferred": self.result.files_transferred,
                "duration_seconds": self.result.duration_seconds,
                "errors": self.result.errors,
            }
            if self.result
            else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobRun":
        """Create from dictionary."""
        return cls(
            job_name=data["job_name"],
            run_id=data["run_id"],
            status=JobStatus(data["status"]),
            started_at=datetime.fromisoformat(data["started_at"]),
            finished_at=datetime.fromisoformat(data["finished_at"])
            if data.get("finished_at")
            else None,
            error_message=data.get("error_message"),
            client_id=data.get("client_id"),
        )


@dataclass
class BackupJob:
    """Executable backup job."""

    config: JobConfig
    _current_run: JobRun | None = field(default=None, repr=False)

    @property
    def name(self) -> str:
        return self.config.name

    def run(
        self,
        dry_run: bool = False,
        progress_callback: Any | None = None,
    ) -> JobRun:
        """Execute the backup job."""
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        started_at = datetime.now()

        self._current_run = JobRun(
            job_name=self.name,
            run_id=run_id,
            status=JobStatus.RUNNING,
            started_at=started_at,
            client_id=self.config.client_id,
        )

        try:
            backend = get_backend("kopia", self.config.backend_options)

            available, message = backend.check_available()
            if not available:
                raise RuntimeError(f"Backend not available: {message}")

            source = BackupSource(
                path=Path(self.config.source.path).expanduser(),
                excludes=self.config.source.excludes,
                includes=self.config.source.includes,
            )

            destination = BackupDestination(path=self.config.destination.path)

            result = backend.backup(
                source=source,
                destination=destination,
                dry_run=dry_run,
                progress_callback=progress_callback,
            )

            self._current_run.finished_at = datetime.now()
            self._current_run.result = result
            self._current_run.status = JobStatus.SUCCESS if result.success else JobStatus.FAILED

            if not result.success and result.errors:
                self._current_run.error_message = "; ".join(result.errors[:3])

            self._save_run_history(self._current_run)
            return self._current_run

        except Exception as e:
            self._current_run.finished_at = datetime.now()
            self._current_run.status = JobStatus.FAILED
            self._current_run.error_message = str(e)
            self._save_run_history(self._current_run)
            return self._current_run

    def restore(
        self,
        target_path: Path,
        snapshot: str | None = None,
        dry_run: bool = False,
    ) -> BackendResult:
        """Restore from this job's backup destination."""
        backend = get_backend("kopia", self.config.backend_options)
        source = BackupDestination(path=self.config.destination.path)
        return backend.restore(source=source, destination=target_path, snapshot=snapshot, dry_run=dry_run)

    def list_snapshots(self) -> list[dict[str, Any]]:
        """List available snapshots for this job."""
        backend = get_backend("kopia", self.config.backend_options)
        destination = BackupDestination(path=self.config.destination.path)
        return backend.list_snapshots(destination)

    def _save_run_history(self, run: JobRun) -> None:
        """Save run to history file."""
        history_dir = get_state_dir() / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        history_file = history_dir / f"{self.name}.jsonl"

        with open(history_file, "a") as f:
            f.write(json.dumps(run.to_dict()) + "\n")

    def get_run_history(self, limit: int = 10) -> list[JobRun]:
        """Get recent run history for this job."""
        history_file = get_state_dir() / "history" / f"{self.name}.jsonl"

        if not history_file.exists():
            return []

        runs = []
        with open(history_file) as f:
            for line in f:
                if line.strip():
                    try:
                        runs.append(JobRun.from_dict(json.loads(line)))
                    except (json.JSONDecodeError, KeyError):
                        pass

        return list(reversed(runs[-limit:]))

