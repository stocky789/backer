"""Data models for the server API."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ClientStatus(str, Enum):
    """Status of a registered client."""

    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class Client(BaseModel):
    """A registered backup client/agent."""

    id: str
    name: str
    hostname: str
    ip_address: str | None = None
    status: ClientStatus = ClientStatus.UNKNOWN
    last_seen: datetime | None = None
    registered_at: datetime = Field(default_factory=datetime.now)
    version: str | None = None
    os_info: str | None = None
    tags: list[str] = Field(default_factory=list)


class JobCreate(BaseModel):
    """Request to create a new backup job."""

    name: str
    source_path: str
    destination_path: str
    backend: str = "rclone"  # rclone or restic (rsync not supported for agents)
    excludes: list[str] = Field(default_factory=list)
    schedule_cron: str | None = None
    client_id: str | None = None
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)
    backend_options: dict[str, Any] = Field(default_factory=dict)


class JobResponse(BaseModel):
    """Response containing job information."""

    name: str
    source_path: str
    destination_path: str
    backend: str
    client_id: str | None
    enabled: bool
    schedule_cron: str | None
    last_run: datetime | None
    last_status: str | None
    next_run: datetime | None


class JobRunRequest(BaseModel):
    """Request to run a job."""

    dry_run: bool = False


class JobRunResponse(BaseModel):
    """Response from running a job."""

    run_id: str
    job_name: str
    status: str
    started_at: datetime
    message: str | None = None


class ClientRegisterRequest(BaseModel):
    """Request from a client to register with the server."""

    hostname: str
    version: str
    os_info: str | None = None
    tags: list[str] = Field(default_factory=list)


class ClientRegisterResponse(BaseModel):
    """Response after successful client registration."""

    client_id: str
    client_secret: str
    server_version: str


class ClientHeartbeat(BaseModel):
    """Heartbeat from client to server."""

    client_id: str
    status: str = "online"
    current_job: str | None = None
    jobs_completed: int = 0
    jobs_failed: int = 0


class BackupCommand(BaseModel):
    """Command sent from server to client to initiate backup."""

    command: str = "backup"  # backup, restore, status
    job_name: str
    source_path: str
    destination_path: str
    backend: str
    excludes: list[str] = Field(default_factory=list)
    backend_options: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False


class BackupResult(BaseModel):
    """Result reported from client after backup."""

    run_id: str
    job_name: str
    client_id: str
    success: bool
    started_at: datetime
    finished_at: datetime
    bytes_transferred: int = 0
    files_transferred: int = 0
    errors: list[str] = Field(default_factory=list)
    output: str = ""
