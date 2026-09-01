"""Unified client configuration."""

import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backer.core.paths import get_config_dir

logger = logging.getLogger(__name__)


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceConfig(ConfigModel):
    path: str
    excludes: list[str] = Field(default_factory=list)
    includes: list[str] = Field(default_factory=list)


class RetentionConfig(ConfigModel):
    keep_last: int | None = None
    keep_daily: int | None = None
    keep_weekly: int | None = None
    keep_monthly: int | None = None
    keep_yearly: int | None = None


class ScheduleConfig(ConfigModel):
    cron: str | None = None
    interval: str | None = None


class ClientConfig(ConfigModel):
    server_url: str = "http://localhost:8420"
    client_id: str = ""
    client_secret: str = ""
    client_secret_ref: str | None = None
    heartbeat_interval: int = 60


class RepositoryOptions(ConfigModel):
    """Reserved typed public repository settings."""


class RepositoryConfig(ConfigModel):
    id: str | None = None
    name: str
    type: str
    path: str | None = None
    server: str | None = None
    share: str | None = None
    username: str | None = None
    domain: str | None = None
    bucket: str | None = None
    prefix: str | None = None
    endpoint: str | None = None
    region: str | None = None
    scope: str | None = None
    unique_id: str | None = None
    added_at: str | None = None
    last_check_status: str | None = None
    last_check_at: str | None = None
    use_existing_session: bool = False
    path_style: bool | None = None
    storage_password_ref: str | None = None
    passphrase_ref: str | None = None
    repository_options: RepositoryOptions | None = None


class JobConfig(ConfigModel):
    repository: str
    source: SourceConfig
    schedule: ScheduleConfig | None = None
    retention: RetentionConfig | None = None
    enabled: bool = True
    pre_scripts: list[str] = Field(default_factory=list)
    post_scripts: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class BackerConfig(ConfigModel):
    agent_id: str = Field(default_factory=lambda: str(uuid4())[:8])
    server: ClientConfig | None = None
    local_scheduled_mode: bool = False
    # ``None`` means no pause.  A past value is deliberately harmless: due_jobs
    # treats it as resumed without making the scheduler write configuration.
    local_scheduled_paused: bool = False
    local_scheduled_pause_until: datetime | None = None
    server_agent_mode: bool = False
    repositories: dict[str, RepositoryConfig] = Field(default_factory=dict)
    jobs: dict[str, JobConfig] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "BackerConfig":
        try:
            with path.open(encoding="utf-8") as file:
                data = yaml.safe_load(file)
        except yaml.YAMLError as error:
            raise ValueError(f"Invalid configuration at {path}: {error}") from error
        try:
            return cls.model_validate(data or {})
        except ValidationError as error:
            raise ValidationError.from_exception_data(f"Invalid configuration at {path}", error.errors()) from error

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                yaml.safe_dump(self.model_dump(exclude_none=True), file, default_flow_style=False, sort_keys=False)
            os.replace(temporary, path)
            if os.name != "nt":
                path.chmod(0o600)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def get_job(self, name: str) -> JobConfig | None:
        return self.jobs.get(name)


def _read_legacy(directory: Path) -> dict[str, str] | None:
    agent = directory / "agent.yaml"
    gui = directory / "config.json"
    if not agent.exists() and not gui.exists():
        return None
    data: dict[str, str] = {}
    if gui.exists():
        import json

        gui_data = json.loads(gui.read_text(encoding="utf-8"))
        data.update({key: value for key, value in gui_data.items() if key != "hostname"})
    if agent.exists():
        loaded = yaml.safe_load(agent.read_text(encoding="utf-8")) or {}
        data.update(
            {key: value for key, value in loaded.items() if key in {"server_url", "client_id", "client_secret"}}
        )
    return data


def migrate_legacy(config_dir: Path | None = None) -> BackerConfig | None:
    """Write the unified file once from legacy agent and GUI credentials."""
    config_dir = config_dir or get_config_dir()
    from backer.core import paths

    candidates = [config_dir, paths._user_config_dir(), paths._machine_config_dir()]
    candidates = list(dict.fromkeys(candidates))
    present = [(directory, data) for directory in candidates if (data := _read_legacy(directory))]
    if not present:
        return None
    chosen_dir, chosen = next((pair for pair in present if pair[0] == config_dir), present[0])
    chosen_id = chosen.get("client_id", "")
    for directory, other in present:
        if directory != chosen_dir and other.get("client_id") and other.get("client_id") != chosen_id:
            logger.warning("Skipping legacy credentials for client_id %s in %s", other["client_id"], directory)
    config = BackerConfig(agent_id=chosen_id or str(uuid4())[:8], server=ClientConfig.model_validate(chosen))
    config.save(config_dir / "config.yaml")
    return config


def load_config(path: Path | None = None) -> BackerConfig:
    path = path or get_config_dir() / "config.yaml"
    if path.exists():
        return BackerConfig.load(path)
    if path.name == "config.yaml":
        migrated = migrate_legacy(path.parent)
        if migrated:
            return migrated
    return BackerConfig()
