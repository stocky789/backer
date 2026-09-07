"""Serverless repository sidecars and read-only job adoption."""

from __future__ import annotations

import getpass
import json
import os
import socket
import sys
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any, NamedTuple

from backer.core.config import BackerConfig, JobConfig, SourceConfig
from backer.core.paths import get_job_subfolder
from backer.core.repo_metadata import legacy_job_subfolder

# Substring match on the key name, lowercased, after '-' is folded to '_': a sidecar is
# plain JSON on a share, so anything credential-shaped must never reach it.
_SECRET_PARTS = (
    "password",
    "passwd",
    "pwd",
    "token",
    "secret",
    "passphrase",
    "access_key",
    "accesskey",
    "api_key",
    "apikey",
    "private_key",
    "credential",
    "creds",
    "connection_string",
    "auth",
    "aws_",
    "signature",
    "session_key",
)
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
    "enabled",
)
# Sidecar job document versions this build can adopt. "1" is the 0.8-era shape, which
# recorded no schedule/retention/excludes; anything higher was written by a newer Backer.
_ADOPTABLE_SCHEMA_VERSIONS = ("1", "2")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _reject_secrets(value: Any, secret_values: set[str], allowed_hint: bool = False) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalised = key.lower().replace("-", "_")
            if any(part in normalised for part in _SECRET_PARTS) and not (
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
        if isinstance(path_hint, str) and (
            Path(path_hint).is_absolute()
            or PureWindowsPath(path_hint).is_absolute()
            or path_hint.startswith("\\\\")
        ):
            repository_hint.pop("path")
    current = current or {}
    if current and current.get("owner_agent_id") != owner_agent_id:
        raise ValueError("only the owning agent can update this sidecar job")
    if current.get("config") == config and current.get("job_name") == job_name and current.get("schema_version") == "2":
        # Nothing about the job changed, so keep the document (and its updated_at) as it
        # is: every run used to rewrite it and move updated_at for no reason.
        return current
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
        "enabled": bool(job.get("enabled", True)),
    }
    options = dict(job.get("repository_options", {}))
    for public_key in ("format", "run_id", "snapshot_id", "cancel_event"):
        options.pop(public_key, None)
    secrets = _secret_values(options) + _secret_values(job.get("smb_password"))
    return build_job_document(job_name, owner, config, secrets, current)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # An identical rewrite only churns mtime (and costs a round trip on a share).
    with suppress(OSError):
        if path.exists() and path.read_text(encoding="utf-8") == json.dumps(value, indent=2):
            return
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


class AdoptOutcome(NamedTuple):
    """What adoption did: what landed, what the operator must act on, what failed."""

    adopted: list[str]
    warnings: list[str]
    failures: dict[str, str]


def _adopt_one(
    config: BackerConfig,
    repository_id: str,
    name: str,
    document: dict[str, Any],
    source: str | None,
    remapped: bool,
    replace_existing: bool,
) -> list[str]:
    """Import one sidecar job, returning its warnings. Raises ValueError to refuse."""
    version = str(document.get("schema_version", "1"))
    if version not in _ADOPTABLE_SCHEMA_VERSIONS:
        raise ValueError(
            f"Job '{name}' uses sidecar schema version {version}, which this Backer does not know how to read; "
            "upgrade Backer on this machine before adopting it"
        )
    existing = config.jobs.get(name)
    if existing and not replace_existing:
        raise ValueError(
            f"A local job named '{name}' already exists (source {existing.source.path}); "
            "adoption would repoint it - pass --replace-existing to overwrite it"
        )
    details = document.get("config", {})
    if not source:
        raise ValueError(f"Job '{name}' has no source path")
    warnings: list[str] = []
    if version == "1":
        warnings.append(
            f"Job '{name}' comes from a pre-0.9 sidecar: schedule, retention and excludes were never recorded, "
            "so it is adopted without them"
        )
    if existing:
        warnings.append(
            f"Job '{name}' replaced the local job whose source was {existing.source.path}; run history and retention "
            f"stay scoped to the previous source and snapshots taken from it are not pruned by this job any more"
        )
    if not remapped and (not Path(source).exists() or details.get("source_platform") not in (None, sys.platform)):
        hint = details.get("repository_hint") or {}
        warnings.append(
            f"Job '{name}' kept the former machine's source path '{source}', which does not exist on this host"
            f" (recorded on {details.get('source_hostname') or 'an unknown host'}/"
            f"{details.get('source_platform') or 'unknown platform'})"
            + (f"; repository hint {json.dumps(hint, sort_keys=True)}" if hint else "")
            + f'; re-run adoption with --source "{name}=<local path>" or the first scheduled run will fail'
        )
    config.jobs[name] = JobConfig(
        repository=repository_id,
        source=SourceConfig(path=source, excludes=details.get("excludes", [])),
        schedule=details.get("schedule"),
        retention=details.get("retention"),
        enabled=details.get("enabled", True),
    )
    return warnings


def adopt_documents(
    config: BackerConfig,
    repository_id: str,
    documents: dict[str, dict[str, Any]],
    names: list[str],
    *,
    source_paths: dict[str, str] | None = None,
    replace_existing: bool = False,
) -> AdoptOutcome:
    """Import already-read sidecar documents without writing them back.

    One unreadable job never costs the others: failures are collected per job.
    """
    source_paths = source_paths or {}
    adopted: list[str] = []
    warnings: list[str] = []
    failures: dict[str, str] = {}
    for name in names:
        document = documents.get(name)
        if not document:
            failures[name] = f"No sidecar job named '{name}'"
            continue
        try:
            warnings.extend(
                _adopt_one(
                    config,
                    repository_id,
                    name,
                    document,
                    source_paths.get(name) or document.get("config", {}).get("source_path"),
                    name in source_paths,
                    replace_existing,
                )
            )
        except Exception as error:  # a malformed legacy document must not abort the rest
            failures[name] = str(error)
            continue
        adopted.append(name)
    return AdoptOutcome(adopted, warnings, failures)


def adopt_jobs(
    config: BackerConfig,
    repository_id: str,
    root: Path,
    names: list[str],
    *,
    source_paths: dict[str, str] | None = None,
    replace_existing: bool = False,
    job_folders: dict[str, str] | None = None,
) -> AdoptOutcome:
    """Copy chosen sidecar jobs locally; never modify the former owner's document."""
    job_folders = job_folders or {}
    documents: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    for name in names:
        # discover_all() also surfaces jobs under Agents/<job>/.backer/, and a sidecar
        # written by pre-0.9 code used a different directory spelling - read wherever
        # the job actually is rather than refusing the whole adoption.
        roots = [root / job_folders[name]] if name in job_folders else [root]
        candidates = [
            base / ".backer" / "jobs" / folder / "config.json"
            for base in roots + [root]
            for folder in dict.fromkeys([get_job_subfolder(name), legacy_job_subfolder(name)])
        ]
        path = next((item for item in candidates if item.exists()), None)
        if path is None:
            failures[name] = f"No sidecar job named '{name}'"
            continue
        try:
            documents[name] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            failures[name] = f"Sidecar job '{name}' is unreadable: {error}"
    outcome = adopt_documents(
        config,
        repository_id,
        documents,
        [name for name in names if name in documents],
        source_paths=source_paths,
        replace_existing=replace_existing,
    )
    return AdoptOutcome(outcome.adopted, outcome.warnings, {**failures, **outcome.failures})
