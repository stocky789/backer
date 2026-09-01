"""Serverless repository setup; creation is always explicit."""

from __future__ import annotations

import socket
import sys
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
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
    if record.type == "smb":
        return "\\\\" + "\\".join(part for part in (record.server, record.share, record.path) if part)
    if not record.path:
        raise ValueError("Repository path is required")
    return record.path


def _backend(record: RepositoryConfig, passphrase: str, storage: dict[str, str] | str | None = None) -> KopiaBackend:
    config: dict[str, object] = {"repository_password": passphrase}
    if record.type == "s3":
        if not isinstance(storage, dict):
            raise ValueError("S3 storage credentials are required")
        config["s3"] = {
            "bucket": record.bucket,
            "prefix": record.prefix or "",
            "endpoint": record.endpoint,
            "region": record.region,
            "access_key_id": storage["access_key_id"],
            "secret_access_key": storage["secret_access_key"],
        }
    return KopiaBackend(config)


def probe(
    record: RepositoryConfig, passphrase: str, storage: dict[str, str] | str | None = None
) -> tuple[str, str | None, str]:
    backend = _backend(record, passphrase, storage)
    status, unique_id = backend.repository_probe(_destination(record))
    return status, unique_id, backend.last_repository_error


def create(record: RepositoryConfig, passphrase: str, storage: dict[str, str] | str | None = None) -> tuple[bool, str]:
    result = _backend(record, passphrase, storage).init_repo(BackupDestination(_destination(record)))
    return result.success, "\n".join(result.errors or [])


def set_maintenance_owner(
    record: RepositoryConfig, passphrase: str, agent_id: str, storage: dict[str, str] | str | None = None
) -> tuple[bool, str]:
    result = _backend(record, passphrase, storage).set_maintenance_owner(
        _destination(record), f"{agent_id}@{socket.gethostname()}"
    )
    return result.success, "\n".join(result.errors or [])


@contextmanager
def repository_operation_context(
    record: RepositoryConfig, storage: dict[str, str] | str | None
) -> Generator[RepositoryConfig, None, None]:
    """Yield the filesystem path Kopia must use for this operation."""
    if getattr(record, "type", None) != "smb":
        yield record
        return
    if sys.platform == "win32":
        if not all((record.server, record.share, record.username)) or (
            not record.use_existing_session and not isinstance(storage, str)
        ):
            raise ValueError("SMB server, share, username and password are required")
        from backer.core.mounts import SMBConnectionManager

        manager = SMBConnectionManager()
        connected = (
            manager.connect_existing_serverless(record.server, record.share, record.path or "")
            if record.use_existing_session
            else manager.connect_serverless(record.server, record.share, record.username, storage, domain=record.domain)
        )
        if not connected:
            raise ValueError(f"Could not connect to SMB repository '{record.name}'")
        try:
            yield record
        finally:
            if getattr(manager, "serverless_session_created", False):
                manager.disconnect_serverless(record.server, record.share)
        return
    if not all((record.server, record.share, record.username)) or not isinstance(storage, str) or not storage:
        raise ValueError("SMB server, share, username and password are required")
    if record.use_existing_session:
        raise ValueError("Linux SMB repositories require a password, not a Windows session")
    raw_path = (record.path or "").replace("\\", "/")
    if PureWindowsPath(raw_path).is_absolute() or raw_path.startswith("/") or ".." in raw_path.split("/"):
        raise ValueError("SMB repository path must be relative to the share")
    from backer.core.mounts import smb_mount_context

    with smb_mount_context(
        record.server or "",
        record.share or "",
        record.username,
        storage if isinstance(storage, str) else None,
        record.domain,
    ) as mount_point:
        yield record.model_copy(update={"type": "local", "path": str(mount_point / raw_path)})


def add_repository(
    config: BackerConfig,
    config_path: Path,
    name: str,
    record: RepositoryConfig,
    passphrase: str,
    *,
    attach: bool,
    init: bool,
    storage: dict[str, str] | str | None = None,
    headless: bool = False,
    adopt: bool = False,
) -> tuple[str, str]:
    if attach and init:
        raise ValueError("Choose exactly one of --attach or --init")
    if not attach and not init:
        raise ValueError("Choose exactly one of --attach or --init")
    if adopt and not init:
        raise ValueError("--adopt requires --init")
    if file_fallback_required() and not headless:
        raise ValueError("No OS keystore is available; re-run with --headless to use protected local files")
    if record.type == "s3":
        parsed = parse_s3_config({**record.model_dump(exclude_none=True), **(storage or {})})
        record = record.model_copy(update={**parsed.public_config, "path": None})
    with repository_operation_context(record, storage) as operation_record:
        status, unique_id, message = probe(operation_record, passphrase, storage)
        if attach and status != "present":
            raise ValueError(message or f"Repository is {status}; nothing was created")
        if init:
            if status == "present":
                if not adopt:
                    raise ValueError("Repository already exists; use --attach or --adopt")
            elif adopt:
                raise ValueError(message or "Repository is absent; adoption requires an existing repository")
            elif status != "absent":
                raise ValueError(message or f"Repository is {status}; refusing to create")
            elif status == "absent":
                created, error = create(operation_record, passphrase, storage)
                if not created:
                    raise ValueError(error or "Repository creation failed")
                status, unique_id, message = probe(operation_record, passphrase, storage)
                if status != "present":
                    raise ValueError(message or "Created repository could not be verified")
                owner_set, owner_error = set_maintenance_owner(operation_record, passphrase, config.agent_id, storage)
                if not owner_set:
                    raise ValueError(owner_error or "Could not set repository maintenance owner")
    repo_id = record.id or uuid4().hex[:12]
    if repo_id in config.repositories:
        raise ValueError(f"Repository id '{repo_id}' already exists")
    passphrase_ref = f"backer/repo/{repo_id}/passphrase"
    storage_ref = f"backer/repo/{repo_id}/storage" if storage else None
    references = [passphrase_ref, storage_ref]
    try:
        backend = keystore.put(passphrase_ref, passphrase)
        if storage_ref:
            import json

            keystore.put(
                storage_ref, json.dumps(storage) if isinstance(storage, dict) else storage, machine_scope=False
            )
        saved = record.model_copy(
            update={
                "id": repo_id,
                "name": name,
                "unique_id": unique_id,
                "added_at": datetime.now(UTC).isoformat(),
                "last_check_status": "present",
                "last_check_at": datetime.now(UTC).isoformat(),
                "passphrase_ref": passphrase_ref,
                "storage_password_ref": storage_ref,
            }
        )
        config.repositories[repo_id] = saved
        config.save(config_path)
    except Exception as error:
        # These refs are fresh, call-scoped names. Compensate every write
        # boundary without touching existing config or repository data.
        config.repositories.pop(repo_id, None)
        cleanup_errors = []
        for reference in references:
            if reference:
                for machine_scope in (False, True):
                    try:
                        keystore.delete(reference, machine_scope=machine_scope)
                    except Exception as cleanup_error:
                        cleanup_errors.append(str(cleanup_error))
        try:
            config.save(config_path)
        except Exception as cleanup_error:
            cleanup_errors.append(str(cleanup_error))
        if cleanup_errors:
            raise RuntimeError(f"{error}; cleanup also failed: {'; '.join(cleanup_errors)}") from error
        raise
    return repo_id, backend


def rescope_secrets_for_system(config: BackerConfig) -> None:
    """Copy every configured repository secret into the machine scope and verify it."""
    for repository_id, record in config.repositories.items():
        for reference in (record.passphrase_ref, record.storage_password_ref):
            if not reference:
                continue
            value = keystore.get(reference, machine_scope=True) or keystore.get(reference)
            if value is None:
                raise ValueError(f"Repository '{record.name or repository_id}' secret cannot be read")
            keystore.put(reference, value, machine_scope=True)
            if keystore.get(reference, machine_scope=True) != value:
                raise ValueError(f"Repository '{record.name or repository_id}' secret cannot be read at machine scope")
        config.repositories[repository_id] = record.model_copy(update={"scope": "machine"})
