"""Translate unified local jobs into the shared runner input."""

from __future__ import annotations

import json
import os
import socket
import sys
from collections.abc import Callable
from contextlib import ExitStack, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backer.backends.base import BackendResult, OperationType
from backer.core import keystore
from backer.core.config import BackerConfig
from backer.core.job import JobRun, JobStatus
from backer.core.messages import failure_needs_input
from backer.core.paths import get_data_dir, get_job_subfolder
from backer.core.runner import run_backup
from backer.serverless.repositories import (
    _format,
    probe,
    repository_operation_context,
)
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


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _clamp_progress(event: dict[str, Any]) -> dict[str, Any]:
    """total_bytes is the previous snapshot's size, so it is an estimate a grown source can exceed."""
    done, total = event.get("bytes_processed"), event.get("total_bytes")
    if done is None or not total:
        return event
    if done > total:
        return {**event, "total_bytes": None, "progress_percent": None}
    if event.get("progress_percent") is not None:
        return {**event, "progress_percent": min(99, event["progress_percent"])}
    return event


def _write_progress(data_dir: Path, run_id: str, **event: Any) -> None:
    _write_json(data_dir / "progress" / f"{run_id}.json", _clamp_progress({"run_id": run_id, **event}))


def _append_live_log(data_dir: Path, run_id: str, line: str) -> None:
    """Append one line to the run log as the backup proceeds, so the UI is not blank.

    Best-effort and never raises: the log is a convenience, never a reason to fail a run.
    The final _write_log rewrites this file with the redacted kopia output when the run ends.
    """
    try:
        path = data_dir / "logs" / f"{run_id}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def _human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{count} B"


def _live_log_frame(data_dir: Path, run_id: str, event: dict[str, Any], state: dict[str, Any]) -> None:
    """Write a readable live-log line, throttled so it does not flood on every frame."""
    hashed = int(event.get("hashed_bytes") or event.get("bytes_processed") or 0)
    files = int(event.get("hashed_files") or event.get("files_processed") or 0)
    # One line per ~64 MiB of new data or per new file count, whichever comes first.
    if hashed - int(state.get("bytes", 0)) < 64 * 1024 * 1024 and files == state.get("files"):
        return
    state["bytes"] = hashed
    state["files"] = files
    _append_live_log(data_dir, run_id, f"Backed up {files} files, {_human_bytes(hashed)} so far")


LOG_HEAD_CHARS = 1000
LOG_TAIL_CHARS = 4000
LOGS_KEPT_PER_JOB = 20


def _write_log(data_dir: Path, run_id: str, job_name: str, text: str, secrets: list[str]) -> None:
    for secret in secrets:
        text = text.replace(secret, "***")
    if len(text) > LOG_HEAD_CHARS + LOG_TAIL_CHARS:
        text = f"{text[:LOG_HEAD_CHARS]}\n...[truncated]...\n{text[-LOG_TAIL_CHARS:]}"
    path = data_dir / "logs" / f"{run_id}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    own = {item.stem for item in (data_dir / "runs" / get_job_subfolder(job_name)).glob("*.json")}
    own.add(run_id)
    mine = [item for item in path.parent.glob("*.log") if item.stem in own]
    for old in sorted(mine, key=lambda item: item.stat().st_mtime, reverse=True)[LOGS_KEPT_PER_JOB:]:
        old.unlink(missing_ok=True)


def _write_preflight_sidecar_run(
    root: Path | None,
    job_name: str,
    run_id: str,
    started: datetime,
    finished: datetime,
    status: str,
    stage: str,
    error_message: str | None,
    agent_id: str | None,
) -> None:
    """Best effort: a failure before the backup stage never reaches the runner's sidecar write."""
    if root is None:
        return
    try:
        from backer.core.repo_metadata import RepositoryMetadata

        repo = RepositoryMetadata(root)
        if not repo.is_initialized():
            return
        repo.save_job_run(
            job_name,
            run_id,
            {
                "status": status,
                "started_at": _utc(started),
                "finished_at": _utc(finished),
                "bytes_transferred": 0,
                "files_transferred": 0,
                "errors": [error_message] if error_message else [],
                "return_code": None,
                "error": " ".join(error_message.splitlines()) if error_message else None,
                "error_stage": stage,
                "snapshot_id": None,
                "agent_id": agent_id,
                "hostname": socket.gethostname(),
            },
        )
    except Exception as error:  # never fail a run over its own bookkeeping
        print(f"[METADATA] Failed to record the pre-flight failure in the sidecar: {error}", file=sys.stderr)


def _run_local_job(
    config: BackerConfig,
    name: str,
    *,
    run_as_system: bool = False,
    on_progress: Callable[..., None] | None = None,
    on_run_id: Callable[[str], None] | None = None,
    cancel_event: Any | None = None,
    process_owner: Any | None = None,
) -> dict[str, Any]:
    suffix = os.environ.get("BACKER_ATTEMPT_TOKEN", "")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + f"-{config.agent_id[:8]}" + (f"-{suffix}" if suffix else "")
    if on_run_id:
        on_run_id(run_id)
    started = datetime.now(UTC)
    data_dir = get_data_dir()
    report: dict[str, Any] = {"run_id": run_id, "job_name": name, "success": False, "errors": []}
    stage = "prepare_destination"
    repository_id = None
    secrets: list[str] = []
    smb_manager = None
    smb_session_created = False
    sidecar_root: Path | None = None
    smb_mounts = ExitStack()
    live_log_state: dict[str, Any] = {"bytes": 0, "files": None}

    def cancelled() -> bool:
        return bool(cancel_event and cancel_event.is_set())

    def cancel_report() -> dict[str, Any]:
        return {**report, "success": False, "cancelled": True, "errors": ["Backup cancelled"]}

    try:
        _write_progress(data_dir, run_id, status="started", started_at=_utc(started))
        _append_live_log(data_dir, run_id, f"Backup of '{name}' started; scanning for changes…")
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
        repository_format = _format(repository)
        machine_scope = run_as_system or repository.scope == "machine"
        if repository.type == "local":
            with suppress(Exception):
                sidecar_root = Path(_destination(repository))
        stage = "keystore"
        passphrase = ""
        options: dict[str, Any] = {"format": repository_format, "run_id": run_id}
        if repository_format == "files":
            options["job_name"] = name
            options["repository_id"] = repository.unique_id
        if repository_format == "kopia":
            passphrase = keystore.get(repository.passphrase_ref or "", machine_scope=machine_scope)
            if not passphrase:
                raise ValueError(f"Repository '{repository.name}' passphrase is unavailable")
            secrets.append(passphrase)
            options["repository_password"] = passphrase
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
        operation_repository = repository
        probe_storage = storage
        if repository.type == "smb" and sys.platform == "win32":
            if run_as_system and repository.use_existing_session and not smb_password:
                raise ValueError(
                    f"Repository '{repository.name}' is interactive-only; add a machine-scoped SMB credential first"
                )
            if (
                not repository.server
                or not repository.share
                or not repository.username
                or (not repository.use_existing_session and not smb_password)
            ):
                raise ValueError(f"Repository '{repository.name}' SMB credentials are incomplete")
            from backer.core.mounts import SMBConnectionManager

            smb_manager = SMBConnectionManager()
            connected = (
                smb_manager.connect_existing_serverless(repository.server, repository.share, repository.path or "")
                if repository.use_existing_session and not run_as_system
                else smb_manager.connect_serverless(
                    repository.server,
                    repository.share,
                    repository.username,
                    smb_password or "",
                    domain=repository.domain,
                    is_system=run_as_system,
                )
            )
            if not connected:
                raise ValueError(f"Could not connect to SMB repository '{repository.name}'")
            smb_session_created = (not repository.use_existing_session or run_as_system) and getattr(
                smb_manager, "serverless_session_created", True
            )
        elif repository.type == "smb":
            operation_repository = smb_mounts.enter_context(repository_operation_context(repository, smb_password))
        if cancelled():
            report = cancel_report()
            return report
        if operation_repository.type != "s3":
            with suppress(Exception):
                sidecar_root = Path(_destination(operation_repository))
        stage = "connect"
        status, unique_id, message = probe(operation_repository, passphrase, probe_storage)
        if status != "present":
            stage = "prepare_destination" if status != "wrong_passphrase" else "connect"
            raise ValueError(message or f"Repository is {status}; backup did not start")
        if repository.unique_id and unique_id != repository.unique_id:
            stage = "prepare_destination"
            raise ValueError(
                f"The repository at {_destination(operation_repository)} is not the one job '{name}' was configured "
                f"against (expected id {repository.unique_id}, found {unique_id or 'none'}); backup did not start. "
                "A repointed share, a replaced disk or a restored-from-empty destination looks like this. "
                "Point the repository record back at the original storage, or add the new one with "
                "'backer repo add' and move the job to it."
            )
        if cancelled():
            report = cancel_report()
            return report
        stage = "backup"
        destination_path = _destination(operation_repository)
        if repository_format == "files":
            destination_path = str(Path(destination_path) / "Agents" / get_job_subfolder(name))
        report = run_backup(
            {
                "serverless": True,
                "run_as_system": run_as_system,
                "run_id": run_id,
                "job_name": name,
                "source_path": job.source.path,
                "excludes": job.source.excludes,
                "enabled": job.enabled,
                "destination_path": destination_path,
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
            on_progress=lambda **event: (
                _write_progress(data_dir, run_id, **{key: value for key, value in event.items() if key != "run_id"}),
                _live_log_frame(data_dir, run_id, event, live_log_state),
                on_progress(**event) if on_progress else None,
            ),
        )
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
        smb_mounts.close()
        finished = datetime.now(UTC)
        error_message = "; ".join(report.get("errors") or []) or None
        report["needs_input"] = bool(error_message and failure_needs_input(error_message))
        status_value = (
            JobStatus.SUCCESS
            if report.get("success")
            else (JobStatus.CANCELLED if report.get("cancelled") else JobStatus.FAILED)
        )
        append_run(
            data_dir,
            JobRun(
                name,
                run_id,
                status_value,
                started,
                finished,
                result=BackendResult(
                    success=bool(report.get("success")),
                    operation=OperationType.BACKUP,
                    started_at=started,
                    finished_at=finished,
                    bytes_transferred=int(report.get("bytes_transferred") or 0),
                    files_transferred=int(report.get("files_transferred") or 0),
                    errors=list(report.get("errors") or []),
                ),
                error_message=error_message,
                client_id=config.agent_id,
                repository_id=repository_id,
                error_stage=None if report.get("success") else stage,
                needs_input=report["needs_input"],
            ),
        )
        if stage != "backup" and not report.get("success"):
            _write_preflight_sidecar_run(
                sidecar_root, name, run_id, started, finished, status_value.value, stage, error_message, config.agent_id
            )
        # run_backup already truncates output to its first 5000 chars, so append the errors the tail would lose.
        outcome = (
            f"Backup completed: {int(report.get('files_transferred') or 0)} files, "
            f"{_human_bytes(int(report.get('bytes_transferred') or 0))}"
            if report.get("success")
            else ("Backup cancelled" if report.get("cancelled") else "Backup failed")
        )
        log_text = "\n".join(part for part in (report.get("output"), error_message, outcome) if part)
        _write_log(data_dir, run_id, name, log_text, secrets)
        (data_dir / "progress" / f"{run_id}.json").unlink(missing_ok=True)
        if smb_manager and smb_session_created:
            smb_manager.disconnect_serverless(repository.server or "", repository.share or "")


def run_local_job(
    config: BackerConfig,
    name: str,
    *,
    run_as_system: bool = False,
    on_progress: Callable[..., None] | None = None,
    on_run_id: Callable[[str], None] | None = None,
    cancel_event: Any | None = None,
    process_owner: Any | None = None,
) -> dict[str, Any] | None:
    """Run one local job only when the shared serverless lock is available."""
    with run_lock(get_data_dir()) as acquired:
        return (
            _run_local_job(
                config,
                name,
                run_as_system=run_as_system,
                on_progress=on_progress,
                on_run_id=on_run_id,
                cancel_event=cancel_event,
                process_owner=process_owner,
            )
            if acquired
            else None
        )


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
