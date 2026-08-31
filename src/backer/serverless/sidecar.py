"""Serverless repository sidecars and read-only job adoption."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backer.core.config import BackerConfig, JobConfig, SourceConfig
from backer.core.paths import get_job_subfolder

_SECRET_PARTS = ("password", "token", "secret", "passphrase", "access_key", "api_key", "private_key", "credential")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _reject_secrets(value: Any, allowed_hint: bool = False) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if any(part in key.lower() for part in _SECRET_PARTS) and not (
                allowed_hint and key == "repository_password_hint"
            ):
                raise ValueError("sidecar job configuration must not contain secrets")
            _reject_secrets(item, allowed_hint)
    elif isinstance(value, list):
        for item in value:
            _reject_secrets(item, allowed_hint)


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
    config = json.loads(json.dumps(config))
    _reject_secrets(config, allowed_hint=True)
    hint = config.get("repository_password_hint")
    if hint and hint in secret_values:
        raise ValueError("repository password hint must not be a secret")
    repository_hint = config.get("repository_hint")
    if isinstance(repository_hint, dict):
        path_hint = repository_hint.get("path")
        if isinstance(path_hint, str) and (Path(path_hint).is_absolute() or path_hint.startswith("\\\\")):
            repository_hint.pop("path")
    path = root / ".backer" / "jobs" / get_job_subfolder(job_name) / "config.json"
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if current and current.get("owner_agent_id") != owner_agent_id:
        raise ValueError("only the owning agent can update this sidecar job")
    now = _utc_now()
    _write_json(
        path,
        {
            "schema_version": "2",
            "job_name": job_name,
            "owner_agent_id": owner_agent_id,
            "created_at": current.get("created_at", now),
            "updated_at": now,
            "config": config,
        },
    )
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
