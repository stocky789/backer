"""Job run record types."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from backer.backends.base import BackendResult


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_name": self.job_name,
            "run_id": self.run_id,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error_message": self.error_message,
            "client_id": self.client_id,
            "repository_id": self.repository_id,
            "error_stage": self.error_stage,
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
        return cls(job_name=data["job_name"], run_id=data["run_id"], status=JobStatus(data["status"]),
                   started_at=datetime.fromisoformat(data["started_at"]),
                   finished_at=datetime.fromisoformat(data["finished_at"]) if data.get("finished_at") else None,
                   error_message=data.get("error_message"), client_id=data.get("client_id"),
                   repository_id=data.get("repository_id"), error_stage=data.get("error_stage"))
