"""Serverless repository setup; creation is always explicit."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from backer.backends.base import BackupDestination
from backer.backends.kopia import KopiaBackend
from backer.backends.s3 import parse_s3_config
from backer.core import keystore
from backer.core.config import BackerConfig, RepositoryConfig
from backer.core.keystore import file_fallback_required


def _destination(record: RepositoryConfig) -> str:
    if record.type == "s3":
        return f"s3://{record.bucket}/{record.prefix or ''}".rstrip("/")
    if not record.path:
        raise ValueError("Repository path is required")
    return record.path


def _backend(record: RepositoryConfig, passphrase: str, storage: dict[str, str] | None = None) -> KopiaBackend:
    config: dict[str, object] = {"repository_password": passphrase}
    if record.type == "s3":
        if not storage:
            raise ValueError("S3 storage credentials are required")
        config["s3"] = {
            "bucket": record.bucket, "prefix": record.prefix or "", "endpoint": record.endpoint,
            "region": record.region, "access_key_id": storage["access_key_id"],
            "secret_access_key": storage["secret_access_key"],
        }
    return KopiaBackend(config)


def probe(
    record: RepositoryConfig, passphrase: str, storage: dict[str, str] | None = None
) -> tuple[str, str | None, str]:
    backend = _backend(record, passphrase, storage)
    status, unique_id = backend.repository_probe(_destination(record))
    return status, unique_id, backend.last_repository_error


def create(record: RepositoryConfig, passphrase: str, storage: dict[str, str] | None = None) -> tuple[bool, str]:
    result = _backend(record, passphrase, storage).init_repo(BackupDestination(_destination(record)))
    return result.success, "\n".join(result.errors or [])


def add_repository(
    config: BackerConfig, config_path: Path, name: str, record: RepositoryConfig, passphrase: str,
    *, attach: bool, init: bool, storage: dict[str, str] | None = None, headless: bool = False,
) -> tuple[str, str]:
    if attach == init:
        raise ValueError("Choose exactly one of --attach or --init")
    if file_fallback_required() and not headless:
        raise ValueError("No OS keystore is available; re-run with --headless to use protected local files")
    if record.type == "s3":
        parsed = parse_s3_config({**record.model_dump(exclude_none=True), **(storage or {})})
        record = record.model_copy(update={**parsed.public_config, "path": None})
    status, unique_id, message = probe(record, passphrase, storage)
    if attach and status != "present":
        raise ValueError(message or f"Repository is {status}; nothing was created")
    if init:
        if status == "present":
            raise ValueError("Repository already exists; use --attach")
        if status != "absent":
            raise ValueError(message or f"Repository is {status}; refusing to create")
        created, error = create(record, passphrase, storage)
        if not created:
            raise ValueError(error or "Repository creation failed")
        status, unique_id, message = probe(record, passphrase, storage)
        if status != "present":
            raise ValueError(message or "Created repository could not be verified")
    repo_id = record.id or uuid4().hex[:12]
    passphrase_ref = f"backer/repo/{repo_id}/passphrase"
    storage_ref = f"backer/repo/{repo_id}/storage" if storage else None
    backend = keystore.put(passphrase_ref, passphrase)
    if storage_ref:
        import json
        keystore.put(storage_ref, json.dumps(storage), machine_scope=False)
    saved = record.model_copy(update={
        "id": repo_id, "name": name, "unique_id": unique_id, "added_at": datetime.now(UTC).isoformat(),
        "last_check_status": "present", "last_check_at": datetime.now(UTC).isoformat(),
        "passphrase_ref": passphrase_ref, "storage_password_ref": storage_ref,
    })
    config.repositories[repo_id] = saved
    config.save(config_path)
    return repo_id, backend
