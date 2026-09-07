"""Serverless repository setup; creation is always explicit."""

from __future__ import annotations

import os
import re
import shutil
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


def _ensure_directory_for_init(record: RepositoryConfig) -> None:
    """--init means "create a repository here", so the folder may not exist yet.

    kopia's filesystem provider does not create it, and the probe reports a missing
    folder as unreachable storage. Only --init creates it; --attach stays fail-closed
    on a path that is not there. A denied write becomes the clearest error this flow
    can produce, instead of kopia's stat message.
    """
    if record.type == "s3":
        return
    destination = _destination(record)
    try:
        os.makedirs(destination, exist_ok=True)
    except PermissionError as error:
        raise ValueError(
            f"The file server denied creating the repository folder {destination}. "
            "Check that your user has write access to the share."
        ) from error
    except OSError as error:
        raise ValueError(f"Could not create the repository folder {destination}: {error}") from error


def _destination(record: RepositoryConfig) -> str:
    if record.type == "s3":
        return f"s3://{record.bucket}/{record.prefix or ''}".rstrip("/")
    if record.type == "smb":
        return "\\\\" + "\\".join(part for part in (record.server, record.share, record.path) if part)
    if not record.path:
        raise ValueError("Repository path is required")
    return record.path


def _format(record: RepositoryConfig) -> str:
    """Read the durable format explicitly; old records remain encrypted Kopia."""
    repository_format = getattr(record, "format", "kopia")
    if repository_format not in {"kopia", "files"}:
        raise ValueError(f"Unsupported repository format: {repository_format}")
    if repository_format == "files" and record.type == "s3":
        raise ValueError("Files repositories do not support S3 storage")
    return repository_format


def _backend(
    record: RepositoryConfig, passphrase: str = "", storage: dict[str, str] | str | None = None
) -> KopiaBackend:
    if _format(record) == "files":
        from backer.backends.files import FilesBackend

        # `id` is the local config key. Only the durable identity discovered
        # from storage may be compared with the marker during later operations.
        return FilesBackend({"repository_id": record.unique_id})
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
    return status, unique_id, getattr(backend, "last_repository_error", "")


def create(record: RepositoryConfig, passphrase: str, storage: dict[str, str] | str | None = None) -> tuple[bool, str]:
    result = _backend(record, passphrase, storage).init_repo(BackupDestination(_destination(record)))
    return result.success, "\n".join(result.errors or [])


def set_maintenance_owner(
    record: RepositoryConfig, passphrase: str, agent_id: str, storage: dict[str, str] | str | None = None
) -> tuple[bool, str]:
    if _format(record) == "files":
        return True, ""
    result = _backend(record, passphrase, storage).set_maintenance_owner(
        _destination(record), f"{agent_id}@{socket.gethostname()}"
    )
    return result.success, "\n".join(result.errors or [])


@contextmanager
def repository_operation_context(
    record: RepositoryConfig, storage: dict[str, str] | str | None
) -> Generator[RepositoryConfig, None, None]:
    """Yield the record/path Kopia must use for this operation.

    SMB repositories always go through Kopia's maintained filesystem provider
    over a mounted path (Windows net-use session, or the Linux reuse -> root
    kernel mount -> gvfs ladder), so this yields a local record pointing at the
    mounted share for the duration of the operation.
    """
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


def destroy_smb_repository(record: RepositoryConfig, passphrase: str, storage: str | None) -> None:
    """Permanently remove one verified Kopia repository directory from an SMB share."""
    if _format(record) != "kopia":
        raise ValueError("Files repository storage deletion is not supported; nothing was deleted")
    raw_path = (record.path or "").replace("\\", "/")
    parts = raw_path.split("/")
    if (
        record.type != "smb"
        or not raw_path
        or "\0" in raw_path
        or PureWindowsPath(raw_path).is_absolute()
        or raw_path.startswith("/")
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise ValueError("Storage deletion requires a non-root SMB repository folder")
    if not record.unique_id:
        raise ValueError("Repository identity is missing; refusing to delete storage")

    with repository_operation_context(record, storage) as operation_record:
        target = Path(_destination(operation_record))
        share_root = target
        for _ in parts:
            share_root = share_root.parent
        if not target.exists() or not target.is_dir():
            raise ValueError("Repository directory is missing; nothing was deleted")
        if target.is_symlink() or (hasattr(target, "is_junction") and target.is_junction()):
            raise ValueError("Repository directory is a link; refusing to delete storage")
        resolved_root = share_root.resolve(strict=True)
        resolved_target = target.resolve(strict=True)
        if resolved_target == resolved_root or resolved_root not in resolved_target.parents:
            raise ValueError("Repository directory escapes the SMB share; refusing to delete storage")

        status, unique_id, message = probe(operation_record, passphrase, storage)
        if status != "present":
            raise ValueError(message or f"Repository is {status}; nothing was deleted")
        if not unique_id or unique_id.casefold() != record.unique_id.casefold():
            raise ValueError("Repository identity changed; nothing was deleted")

        shutil.rmtree(target)
        if target.exists():
            raise OSError("Repository directory still exists after deletion")


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
    repository_format = _format(record)
    if repository_format == "files" and passphrase:
        raise ValueError("Files repositories do not use a passphrase")
    if repository_format == "kopia" and file_fallback_required() and not headless:
        raise ValueError("No OS keystore is available; re-run with --headless to use protected local files")
    if record.type == "s3":
        parsed = parse_s3_config({**record.model_dump(exclude_none=True), **(storage or {})})
        record = record.model_copy(update={**parsed.public_config, "path": None})
    # kopia reports an existing-but-undecryptable repository as "wrong_passphrase": the format
    # blob is there, this passphrase just does not open it. For the operator that means "a
    # repository already exists here", so both present and wrong_passphrase are "already exists".
    already_exists = (
        "A repository already exists at this location. To use it, choose \"Connect to a "
        "repository that already exists\" and enter its own passphrase; to create a new "
        "repository, choose an empty folder."
    )
    wrong_passphrase_for_existing = (
        "A repository already exists at this location, but the passphrase entered does not open "
        "it. Enter the passphrase this repository was created with, or choose an empty folder to "
        "create a new one."
    )
    with repository_operation_context(record, storage) as operation_record:
        if init:
            _ensure_directory_for_init(operation_record)
        status, unique_id, message = probe(operation_record, passphrase, storage)
        if attach:
            if status == "wrong_passphrase":
                raise ValueError(wrong_passphrase_for_existing)
            if status != "present":
                raise ValueError(message or f"Repository is {status}; nothing was created")
        if init:
            if status == "present":
                if not adopt:
                    raise ValueError(already_exists)
            elif status == "wrong_passphrase":
                raise ValueError(wrong_passphrase_for_existing)
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
                if repository_format == "kopia":
                    owner_set, owner_error = set_maintenance_owner(
                        operation_record, passphrase, config.agent_id, storage
                    )
                    if not owner_set:
                        raise ValueError(owner_error or "Could not set repository maintenance owner")
    repo_id = record.id or uuid4().hex[:12]
    if repo_id in config.repositories:
        raise ValueError(f"Repository id '{repo_id}' already exists")
    passphrase_ref = f"backer/repo/{repo_id}/passphrase" if repository_format == "kopia" else None
    storage_ref = f"backer/repo/{repo_id}/storage" if storage else None
    references = [passphrase_ref, storage_ref]
    try:
        backend = keystore.put(passphrase_ref, passphrase) if passphrase_ref else "none"
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


def recovery_record(
    name: str,
    location: str,
    passphrase: str,
    created_at: str | None = None,
    connect_command: str | None = None,
    credential_instruction: str | None = None,
) -> str:
    """Build the explicitly requested plaintext record needed to reconnect elsewhere."""
    created_at = created_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    lines = [
        "Backer recovery record",
        f"Repository: {name}",
        f"Location: {location}",
        f"Created (UTC): {created_at}",
        f"Passphrase: {passphrase}",
        connect_command or f'kopia repository connect filesystem --path "{location}" --no-persist-credentials',
    ]
    if credential_instruction:
        lines.append(credential_instruction)
    return "\n".join((*lines, ""))


def passphrase_words(value: str) -> list[str]:
    """Use the same readable positions for generated and user-supplied phrases."""
    return [word for word in re.split(r"[\s-]+", value.strip()) if word]


def valid_supplied_passphrase(candidate: str, confirmation: str) -> bool:
    """Keep the user-supplied route as strict as the generated confirmation route."""
    return bool(passphrase_words(candidate)) and candidate == confirmation


def confirmation_word(value: str, position: int) -> str:
    return passphrase_words(value)[position - 1]


def connection_conflict_message(server: str, conflict=None) -> str:
    """Name the existing Windows SMB connection; never hide it behind error 1219."""
    from backer.core.mounts import SMBConnectionManager

    conflict = conflict or SMBConnectionManager()._find_existing_connection(server)
    if conflict:
        share, username = conflict
        return (
            f"Windows is already connected to {share} as {username or 'another account'}. Close it or use that account."
        )
    return "Windows already has a connection to this server with different credentials."


def rollback_repository(config, config_path, repository_id: str) -> list[str]:
    """Undo only this invocation's local record and refs; report every failure."""
    record = config.repositories.pop(repository_id, None)
    errors = []
    if record:
        for reference in (record.passphrase_ref, record.storage_password_ref):
            if not reference:
                continue
            for machine_scope in (False, True):
                try:
                    keystore.delete(reference, machine_scope=machine_scope)
                except Exception as error:
                    errors.append(f"Could not remove repository secret: {error}")
    try:
        config.save(config_path)
    except Exception as error:
        errors.append(f"Could not save repository rollback: {error}")
    return errors
