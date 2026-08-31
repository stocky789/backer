"""Serverless repository sidecars and read-only job adoption."""

from __future__ import annotations

import getpass
import json
import os
import socket
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backer.core.config import BackerConfig, JobConfig, SourceConfig
from backer.core.paths import get_job_subfolder

_SECRET_PARTS = ("password", "token", "secret", "passphrase", "access_key", "api_key", "private_key", "credential")
_JOB_CONFIG_FIELDS = (
    "source_path",
    "source_hostname",
    "source_platform",
    "kopia_source",
    "excludes",
    "subfolder",
    "schedule",
    "retention",
    "repository_hint",
    "repository_password_hint",
    "client_id",
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _reject_secrets(value: Any, secret_values: set[str], allowed_hint: bool = False) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if any(part in key.lower() for part in _SECRET_PARTS) and not (
                allowed_hint and key == "repository_password_hint"
            ):
                raise ValueError("sidecar job configuration must not contain secrets")
            _reject_secrets(item, secret_values, allowed_hint)
    elif isinstance(value, list):
        for item in value:
            _reject_secrets(item, secret_values, allowed_hint)
    elif isinstance(value, str) and value in secret_values:
        raise ValueError("sidecar job configuration must not contain secrets")


def build_job_document(
    job_name: str,
    owner_agent_id: str,
    config: dict[str, Any],
    secret_values: list[str],
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and build the one portable serverless job document."""
    config = json.loads(json.dumps(config))
    _reject_secrets(config, {value for value in secret_values if value}, allowed_hint=True)
    missing = [field for field in _JOB_CONFIG_FIELDS if field not in config]
    if missing:
        raise ValueError(f"sidecar job configuration missing: {', '.join(missing)}")
    repository_hint = config["repository_hint"]
    if isinstance(repository_hint, dict):
        path_hint = repository_hint.get("path")
        if isinstance(path_hint, str) and (Path(path_hint).is_absolute() or path_hint.startswith("\\\\")):
            repository_hint.pop("path")
    current = current or {}
    if current and current.get("owner_agent_id") != owner_agent_id:
        raise ValueError("only the owning agent can update this sidecar job")
    now = _utc_now()
    return {
        "schema_version": "2",
        "job_name": job_name,
        "owner_agent_id": owner_agent_id,
        "created_at": current.get("created_at", now),
        "updated_at": now,
        "config": config,
    }


def _secret_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [secret for item in value.values() for secret in _secret_values(item)]
    if isinstance(value, (list, tuple)):
        return [secret for item in value for secret in _secret_values(item)]
    return [value] if isinstance(value, str) else []


def build_serverless_job_document(
    job: dict[str, Any], job_name: str, source_path: str, agent_id: str | None, current: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build the complete B2 sidecar document for every repository type."""
    owner = agent_id or "unknown"
    hostname = socket.gethostname()
    config = {
        "source_path": source_path,
        "source_hostname": hostname,
        "source_platform": sys.platform,
        "kopia_source": f"{getpass.getuser()}@{hostname}:{source_path}",
        "excludes": job.get("excludes", []),
        "subfolder": get_job_subfolder(job_name),
        "schedule": job.get("schedule"),
        "retention": job.get("retention"),
        "repository_hint": job.get("repository_hint", {}),
        "repository_password_hint": job.get("repository_password_hint"),
        "client_id": agent_id,
    }
    secrets = _secret_values(job.get("repository_options", {})) + _secret_values(job.get("smb_password"))
    return build_job_document(job_name, owner, config, secrets, current)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(value, file, indent=2)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def save_job_config(
    root: Path, job_name: str, owner_agent_id: str, config: dict[str, Any], secret_values: list[str]
) -> Path:
    """Write an owner-only, secret-free job definition at the stable sidecar path."""
    path = root / ".backer" / "jobs" / get_job_subfolder(job_name) / "config.json"
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    _write_json(path, build_job_document(job_name, owner_agent_id, config, secret_values, current))
    return path


def adopt_jobs(
    config: BackerConfig,
    repository_id: str,
    root: Path,
    names: list[str],
    *,
    source_paths: dict[str, str] | None = None,
) -> list[str]:
    """Copy chosen sidecar jobs locally; never modify the former owner's document."""
    source_paths = source_paths or {}
    adopted: list[str] = []
    for name in names:
        path = root / ".backer" / "jobs" / get_job_subfolder(name) / "config.json"
        if not path.exists():
            raise ValueError(f"No sidecar job named '{name}'")
        document = json.loads(path.read_text(encoding="utf-8"))
        details = document.get("config", {})
        source = source_paths.get(name, details.get("source_path"))
        if not source:
            raise ValueError(f"Job '{name}' has no source path")
        config.jobs[name] = JobConfig(
            repository=repository_id,
            source=SourceConfig(path=source, excludes=details.get("excludes", [])),
            schedule=details.get("schedule"),
            retention=details.get("retention"),
            enabled=details.get("enabled", True),
        )
        adopted.append(name)
    return adopted


def adopt_documents(
    config: BackerConfig,
    repository_id: str,
    documents: dict[str, dict[str, Any]],
    names: list[str],
    *,
    source_paths: dict[str, str] | None = None,
) -> list[str]:
    """Import already-read sidecar documents without writing them back."""
    source_paths = source_paths or {}
    adopted: list[str] = []
    for name in names:
        document = documents.get(name)
        if not document:
            raise ValueError(f"No sidecar job named '{name}'")
        details = document.get("config", {})
        source = source_paths.get(name, details.get("source_path"))
        if not source:
            raise ValueError(f"Job '{name}' has no source path")
        config.jobs[name] = JobConfig(
            repository=repository_id,
            source=SourceConfig(path=source, excludes=details.get("excludes", [])),
            schedule=details.get("schedule"),
            retention=details.get("retention"),
            enabled=details.get("enabled", True),
        )
        adopted.append(name)
    return adopted
