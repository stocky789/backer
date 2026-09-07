"""Job run record types."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from backer.backends.base import BackendResult, OperationType


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class JobRun:
    job_name: str
    run_id: str
    status: JobStatus
    started_at: datetime
    finished_at: datetime | None = None
    result: BackendResult | None = None
    error_message: str | None = None
    client_id: str | None = None
    repository_id: str | None = None
    error_stage: str | None = None
    needs_input: bool = False

    def to_dict(self) -> dict[str, Any]:
        def utc(value: datetime | None) -> str | None:
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None

        return {
            "job_name": self.job_name,
            "run_id": self.run_id,
            "status": self.status.value,
            "started_at": utc(self.started_at),
            "finished_at": utc(self.finished_at),
            "error_message": self.error_message,
            "client_id": self.client_id,
            "repository_id": self.repository_id,
            "error_stage": self.error_stage,
            "needs_input": self.needs_input,
            "result": {
                "success": self.result.success,
                "bytes_transferred": self.result.bytes_transferred,
                "files_transferred": self.result.files_transferred,
                "duration_seconds": self.result.duration_seconds,
                "errors": self.result.errors,
            } if self.result else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobRun":
        started_at = datetime.fromisoformat(data["started_at"])
        finished_at = datetime.fromisoformat(data["finished_at"]) if data.get("finished_at") else None
        result = data.get("result")
        return cls(job_name=data["job_name"], run_id=data["run_id"], status=JobStatus(data["status"]),
                   started_at=started_at, finished_at=finished_at,
                   result=BackendResult(
                       success=bool(result.get("success")),
                       operation=OperationType.BACKUP,
                       started_at=started_at,
                       # duration_seconds is derived, so rebuild finished_at from it rather than the run's.
                       finished_at=started_at + timedelta(seconds=float(result.get("duration_seconds") or 0.0)),
                       bytes_transferred=int(result.get("bytes_transferred") or 0),
                       files_transferred=int(result.get("files_transferred") or 0),
                       errors=list(result.get("errors") or []),
                   ) if result else None,
                   error_message=data.get("error_message"), client_id=data.get("client_id"),
                   repository_id=data.get("repository_id"), error_stage=data.get("error_stage"),
                   needs_input=bool(data.get("needs_input", False)))
