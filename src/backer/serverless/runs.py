"""Translate unified local jobs into the shared runner input."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from backer.core import keystore
from backer.core.config import BackerConfig
from backer.core.paths import get_data_dir
from backer.core.runner import run_backup
from backer.serverless.schedule import due_jobs


def _destination(repository: Any) -> str:
    if repository.type == "s3":
        return f"s3://{repository.bucket}/{repository.prefix or ''}".rstrip("/")
    if repository.type == "smb":
        return "\\\\" + "\\".join(part for part in (repository.server, repository.share, repository.path) if part)
    if not repository.path:
        raise ValueError(f"Repository '{repository.name}' has no path")
    return repository.path


def run_local_job(config: BackerConfig, name: str, *, run_as_system: bool = False) -> dict[str, Any]:
    job = config.jobs.get(name)
    if not job:
        raise ValueError(f"Job '{name}' is not configured")
    repository = config.repositories.get(job.repository)
    if not repository:
        raise ValueError(f"Job '{name}' names an unknown repository")
    machine_scope = run_as_system or repository.scope == "machine"
    passphrase = keystore.get(repository.passphrase_ref or "", machine_scope=machine_scope)
    if not passphrase:
        raise ValueError(f"Repository '{repository.name}' passphrase is unavailable")
    options: dict[str, Any] = {"repository_password": passphrase}
    if repository.type == "s3":
        raw = keystore.get(repository.storage_password_ref or "", machine_scope=machine_scope)
        if not raw:
            raise ValueError(f"Repository '{repository.name}' storage credential is unavailable")
        options["s3"] = {
            **json.loads(raw),
            "bucket": repository.bucket,
            "prefix": repository.prefix or "",
            "endpoint": repository.endpoint,
            "region": repository.region,
        }
    smb_password = keystore.get(repository.storage_password_ref or "", machine_scope=machine_scope)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + f"-{config.agent_id[:8]}"
    return run_backup(
        {
            "serverless": True,
            "run_as_system": run_as_system,
            "run_id": run_id,
            "job_name": name,
            "source_path": job.source.path,
            "excludes": job.source.excludes,
            "destination_path": _destination(repository),
            "repository_options": options,
            "smb_username": repository.username,
            "smb_password": smb_password,
            "smb_domain": repository.domain,
            "repository_id": job.repository,
            "schedule": job.schedule.model_dump() if job.schedule else None,
            "retention": job.retention.model_dump() if job.retention else None,
            "repository_hint": {
                key: value
                for key, value in repository.model_dump(exclude_none=True).items()
                if key
                not in {
                    "id",
                    "name",
                    "unique_id",
                    "added_at",
                    "last_check_status",
                    "last_check_at",
                    "storage_password_ref",
                    "passphrase_ref",
                }
            },
        },
        agent_credentials=(config.agent_id, ""),
    )


def run_due_jobs(
    config: BackerConfig, now: datetime | None = None, *, run_as_system: bool = False
) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    return [run_local_job(config, name, run_as_system=run_as_system) for name in due_jobs(config, now, get_data_dir())]
