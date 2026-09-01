"""Translate unified local jobs into the shared runner input."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backer.core import keystore
from backer.core.config import BackerConfig
from backer.core.job import JobRun, JobStatus
from backer.core.paths import get_data_dir
from backer.core.runner import run_backup
from backer.serverless.repositories import probe
from backer.serverless.schedule import due_jobs, run_lock
from backer.serverless.store import _write_json, append_run


def _destination(repository: Any) -> str:
    if repository.type == "s3":
        return f"s3://{repository.bucket}/{repository.prefix or ''}".rstrip("/")
    if repository.type == "smb":
        return "\\\\" + "\\".join(part for part in (repository.server, repository.share, repository.path) if part)
    if not repository.path:
        raise ValueError(f"Repository '{repository.name}' has no path")
    return repository.path


def _write_progress(data_dir: Path, run_id: str, **event: Any) -> None:
    _write_json(data_dir / "progress" / f"{run_id}.json", {"run_id": run_id, **event})


def _write_log(data_dir: Path, run_id: str, text: str, secrets: list[str]) -> None:
    for secret in secrets:
        text = text.replace(secret, "***")
    path = data_dir / "logs" / f"{run_id}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text[:5000], encoding="utf-8")
    for old in sorted(path.parent.glob("*.log"), key=lambda item: item.stat().st_mtime, reverse=True)[20:]:
        old.unlink(missing_ok=True)


def _run_local_job(
    config: BackerConfig, name: str, *, run_as_system: bool = False, on_progress: Callable[..., None] | None = None,
    cancel_event: Any | None = None, process_owner: Any | None = None,
) -> dict[str, Any]:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + f"-{config.agent_id[:8]}"
    started = datetime.now(UTC)
    data_dir = get_data_dir()
    report: dict[str, Any] = {"run_id": run_id, "job_name": name, "success": False, "errors": []}
    stage = "prepare_destination"
    repository_id = None
    secrets: list[str] = []
    smb_manager = None
    smb_session_created = False

    def cancelled() -> bool:
        return bool(cancel_event and cancel_event.is_set())

    def cancel_report() -> dict[str, Any]:
        return {**report, "success": False, "cancelled": True, "errors": ["Backup cancelled"]}

    try:
        _write_progress(data_dir, run_id, status="started", started_at=started.isoformat().replace("+00:00", "Z"))
        if cancelled():
            report = cancel_report()
            return report
        job = config.jobs.get(name)
        if not job:
            raise ValueError(f"Job '{name}' is not configured")
        repository_id = job.repository
        repository = config.repositories.get(job.repository)
        if not repository:
            raise ValueError(f"Job '{name}' names an unknown repository")
        machine_scope = run_as_system or repository.scope == "machine"
        stage = "keystore"
        passphrase = keystore.get(repository.passphrase_ref or "", machine_scope=machine_scope)
        if not passphrase:
            raise ValueError(f"Repository '{repository.name}' passphrase is unavailable")
        secrets.append(passphrase)
        options: dict[str, Any] = {"repository_password": passphrase}
        if process_owner is not None:
            options["process_owner"] = process_owner
        storage = None
        if repository.type == "s3":
            raw = keystore.get(repository.storage_password_ref or "", machine_scope=machine_scope)
            if not raw:
                raise ValueError(f"Repository '{repository.name}' storage credential is unavailable")
            storage = json.loads(raw)
            secrets.extend(str(value) for value in storage.values())
            options["s3"] = {
                **storage,
                "bucket": repository.bucket,
                "prefix": repository.prefix or "",
                "endpoint": repository.endpoint,
                "region": repository.region,
            }
        smb_password = (
            keystore.get(repository.storage_password_ref, machine_scope=machine_scope)
            if repository.storage_password_ref
            else None
        )
        if smb_password:
            secrets.append(smb_password)
        if repository.type == "smb":
            if run_as_system and repository.use_existing_session and not smb_password:
                raise ValueError(
                    f"Repository '{repository.name}' is interactive-only; add a machine-scoped SMB credential first"
                )
            if not repository.server or not repository.share or not repository.username or (
                not repository.use_existing_session and not smb_password
            ):
                raise ValueError(f"Repository '{repository.name}' SMB credentials are incomplete")
            from backer.core.mounts import SMBConnectionManager

            smb_manager = SMBConnectionManager()
            connected = (
                smb_manager.connect_existing_serverless(repository.server, repository.share, repository.path or "")
                if repository.use_existing_session and not run_as_system
                else smb_manager.connect_serverless(
                    repository.server, repository.share, repository.username, smb_password or "",
                    domain=repository.domain, is_system=run_as_system,
                )
            )
            if not connected:
                raise ValueError(f"Could not connect to SMB repository '{repository.name}'")
            smb_session_created = (
                not repository.use_existing_session or run_as_system
            ) and getattr(smb_manager, "serverless_session_created", True)
        if cancelled():
            report = cancel_report()
            return report
        stage = "connect"
        status, unique_id, message = probe(repository, passphrase, storage)
        if status != "present" or (repository.unique_id and unique_id != repository.unique_id):
            stage = "prepare_destination" if status != "wrong_passphrase" else "connect"
            raise ValueError(message or f"Repository is {status}; backup did not start")
        if cancelled():
            report = cancel_report()
            return report
        stage = "backup"
        report = run_backup({
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
        }, agent_credentials=(config.agent_id, ""), on_progress=lambda **event: (
            _write_progress(data_dir, run_id, **{key: value for key, value in event.items() if key != "run_id"}),
            on_progress(**event) if on_progress else None,
        ))
        if cancelled():
            report = cancel_report()
            return report
        if not report["success"]:
            raise RuntimeError("; ".join(report.get("errors") or ["Backup failed"]))
        return report
    except KeyboardInterrupt:
        report = cancel_report() if cancelled() else {**report, "success": False, "errors": ["Backup interrupted"]}
        return report
    except Exception as error:
        if cancelled():
            report = cancel_report()
            return report
        text = str(error)
        for secret in secrets:
            text = text.replace(secret, "***")
        report = {**report, "success": False, "errors": [text]}
        return report
    finally:
        finished = datetime.now(UTC)
        error_message = "; ".join(report.get("errors") or []) or None
        append_run(data_dir, JobRun(name, run_id, JobStatus.SUCCESS if report.get("success") else (
            JobStatus.CANCELLED if report.get("cancelled") else JobStatus.FAILED),
                                   started, finished, error_message=error_message, client_id=config.agent_id,
                                   repository_id=repository_id, error_stage=None if report.get("success") else stage))
        _write_log(data_dir, run_id, report.get("output") or error_message or "", secrets)
        (data_dir / "progress" / f"{run_id}.json").unlink(missing_ok=True)
        if smb_manager and smb_session_created:
            smb_manager.disconnect_serverless(repository.server or "", repository.share or "")


def run_local_job(
    config: BackerConfig, name: str, *, run_as_system: bool = False, on_progress: Callable[..., None] | None = None,
    cancel_event: Any | None = None, process_owner: Any | None = None,
) -> dict[str, Any] | None:
    """Run one local job only when the shared serverless lock is available."""
    with run_lock(get_data_dir()) as acquired:
        return _run_local_job(
            config, name, run_as_system=run_as_system, on_progress=on_progress,
            cancel_event=cancel_event, process_owner=process_owner,
        ) if acquired else None


def run_due_jobs(
    config: BackerConfig, now: datetime | None = None, *, run_as_system: bool = False
) -> list[dict[str, Any]] | None:
    now = now or datetime.now(UTC)
    with run_lock(get_data_dir()) as acquired:
        if not acquired:
            return None
        return [
            _run_local_job(config, name, run_as_system=run_as_system) for name in due_jobs(config, now, get_data_dir())
        ]
