"""Configuration management for Backer."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class SourceConfig(BaseModel):
    """Configuration for a backup source."""

    path: str
    excludes: list[str] = Field(default_factory=list)
    includes: list[str] = Field(default_factory=list)


class DestinationConfig(BaseModel):
    """Configuration for a backup destination."""

    path: str


class RetentionConfig(BaseModel):
    """Retention policy configuration."""

    keep_last: int | None = None
    keep_daily: int | None = None
    keep_weekly: int | None = None
    keep_monthly: int | None = None
    keep_yearly: int | None = None


class ScheduleConfig(BaseModel):
    """Schedule configuration for a job."""

    cron: str | None = None  # Cron expression
    interval: str | None = None  # Alternative: "hourly", "daily", "weekly"


class JobConfig(BaseModel):
    """Configuration for a backup job."""

    name: str
    source: SourceConfig
    destination: DestinationConfig
    schedule: ScheduleConfig | None = None
    retention: RetentionConfig | None = None
    enabled: bool = True
    pre_scripts: list[str] = Field(default_factory=list)
    post_scripts: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    client_id: str | None = None  # Which client this job runs on


class ServerConfig(BaseModel):
    """Server-specific configuration."""

    host: str = "0.0.0.0"
    port: int = 8420
    secret_key: str = ""  # For signing tokens
    data_dir: str = "/var/lib/backer"
    allowed_origins: list[str] = Field(default_factory=lambda: ["*"])
    public_url: str = "http://localhost:8420"  # Public URL for reverse proxy (Cloudflare, nginx, etc.)


class ClientConfig(BaseModel):
    """Client/agent-specific configuration."""

    server_url: str = "http://localhost:8420"
    client_id: str = ""
    client_secret: str = ""
    heartbeat_interval: int = 60  # seconds


class BackerConfig(BaseModel):
    """Main Backer configuration."""

    version: str = "1"
    mode: str = "standalone"  # standalone, server, client
    server: ServerConfig = Field(default_factory=ServerConfig)
    client: ClientConfig = Field(default_factory=ClientConfig)
    jobs: list[JobConfig] = Field(default_factory=list)
    defaults: dict[str, Any] = Field(default_factory=dict)
    log_level: str = "info"
    log_file: str | None = None

    @classmethod
    def load(cls, path: Path) -> "BackerConfig":
        """Load configuration from YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls.model_validate(data or {})

    def save(self, path: Path) -> None:
        """Save configuration to YAML file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            yaml.dump(self.model_dump(exclude_none=True), f, default_flow_style=False)

    def get_job(self, name: str) -> JobConfig | None:
        """Get a job by name."""
        for job in self.jobs:
            if job.name == name:
                return job
        return None


def get_default_config_path() -> Path:
    """Get the default configuration file path."""
    xdg_config = Path.home() / ".config" / "backer"
    return xdg_config / "config.yaml"


def get_state_dir() -> Path:
    """Get the state directory for backer data."""
    state_dir = Path.home() / ".local" / "share" / "backer"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def load_config(path: Path | None = None) -> BackerConfig:
    """Load configuration from file, or return default config."""
    if path is None:
        path = get_default_config_path()

    if path.exists():
        return BackerConfig.load(path)

    return BackerConfig()

