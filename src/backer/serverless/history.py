"""Run history and repository presentation helpers shared by the CLI and desktop clients."""

from __future__ import annotations

import json
from pathlib import Path

from backer.core import keystore
from backer.core.config import BackerConfig
from backer.core.repo_metadata import RepositoryMetadata


def repository_details(config: BackerConfig, repository_id: str) -> str:
    record = config.repositories.get(repository_id)
    if not record:
        return "Repository unavailable"
    if record.type == "s3":
        location = f"s3://{record.bucket}/{record.prefix or ''}".rstrip("/")
    elif record.type == "smb":
        location = "\\\\" + "\\".join(part for part in (record.server, record.share, record.path) if part)
    else:
        location = record.path or "location unavailable"
    state = "passphrase stored" if record.passphrase_ref else "passphrase unavailable"
    return f"{record.name} · {record.type} · {location} · {state}"


def _run_started(run) -> str:
    if isinstance(run, dict):
        return str(run.get("started_at") or run.get("recorded_at") or "")
    return run.started_at.isoformat() if getattr(run, "started_at", None) else ""


def _run_status(run) -> str:
    status = run.get("status") if isinstance(run, dict) else getattr(run, "status", None)
    return getattr(status, "value", status or "never").replace("_", " ").title()


def _run_bytes(run) -> int:
    if isinstance(run, dict):
        return int(run.get("bytes_transferred") or (run.get("result") or {}).get("bytes_transferred") or 0)
    result = getattr(run, "result", None)
    return int(getattr(result, "bytes_transferred", 0) or 0)


def _size(value: int) -> str:
    if not value:
        return "—"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units[:-1]:
        if amount < 1024:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} {units[-1]}"


def run_summary(local, repository) -> tuple[str, str]:
    """Select the newest local or repository record without treating pending data as zero."""
    runs = [run for run in (local, repository) if run is not None]
    if not runs:
        return "Never run", "—"
    newest = max(runs, key=_run_started)
    return _run_status(newest), _size(_run_bytes(newest))


def repository_location(repository) -> Path | None:
    if repository.type == "local" and repository.path:
        return Path(repository.path)
    if repository.type == "smb" and repository.server and repository.share:
        tail = (repository.path or "").strip("\\/")
        return Path("\\\\" + repository.server + "\\" + repository.share + ("\\" + tail if tail else ""))
    return None


def repository_history(repository, job_name: str) -> list[dict[str, object]]:
    """Read the repository's own run sidecar for every storage type."""
    if repository.type == "s3":
        raw = keystore.get(repository.storage_password_ref or "", machine_scope=repository.scope == "machine")
        if not raw:
            return []
        from backer.core.paths import get_job_subfolder
        from backer.serverless.s3_sidecar import S3Sidecar

        sidecar = S3Sidecar(repository.model_dump(exclude_none=True), json.loads(raw))
        prefix = f".backer/jobs/{get_job_subfolder(job_name)}/runs/"
        strip = (repository.prefix or "").strip("/") + "/"
        records = []
        for key in sidecar.list(prefix):
            key = key.removeprefix(strip)
            if key.endswith(".json") and (payload := sidecar.get(key)):
                records.append(json.loads(payload))
        return sorted(records, key=lambda item: str(item.get("started_at") or ""), reverse=True)
    if location := repository_location(repository):
        return RepositoryMetadata(location).get_job_runs(job_name, 1)
    return []
