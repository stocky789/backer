"""Local scheduling mode changes with full rollback of files, secrets, and scheduler state."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit, urlunsplit

from backer.core import keystore
from backer.core.config import BackerConfig, ClientConfig
from backer.core.config import load_config as _load_config
from backer.core.paths import _user_config_dir, get_config_dir, get_machine_config_dir


class ModeApplyResult(NamedTuple):
    ok: bool
    config: BackerConfig
    message: str


def _restore_config_file(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def load_config() -> BackerConfig:
    """Load the durable local configuration from the resolved config directory."""
    return _load_config()


def save_config(config: BackerConfig) -> None:
    """Save the durable local configuration to the resolved config directory."""
    config.save(get_config_dir() / "config.yaml")


def get_user_config_dir() -> Path:
    """Return the interactive configuration location without machine fallback."""
    return _user_config_dir()


def unattended_blocker(config: BackerConfig) -> str | None:
    """Keep SYSTEM setup fail-closed when an SMB repository only has an interactive session."""
    for repository in config.repositories.values():
        if repository.type == "smb" and repository.use_existing_session and not repository.storage_password_ref:
            return f"Repository '{repository.name}' is interactive-only; add a machine-scoped SMB credential first"
    return None


def normalize_server_url(value: str) -> str:
    """Match the established desktop connection defaults without guessing a custom port."""
    value = value.strip()
    if not value.startswith(("http://", "https://")):
        value = "http://" + value
    parts = urlsplit(value)
    if not parts.hostname:
        raise ValueError("Enter a server address")
    netloc = parts.netloc if parts.port is not None else f"{parts.netloc}:8420"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, "")).rstrip("/")


def settings_update(config: BackerConfig, value: str) -> BackerConfig:
    """Return one durable settings update without discarding registered credentials."""
    server = config.server
    if value.strip():
        current = server or ClientConfig()
        server = current.model_copy(update={"server_url": normalize_server_url(value)})
    return config.model_copy(update={"server": server})


def local_schedule_configured() -> bool:
    """The installed platform trigger is the authoritative local-schedule mode."""
    from backer.client.windows_service import snapshot_local_scheduler

    snapshot = snapshot_local_scheduler()
    if snapshot.get("platform") == "windows":
        task = snapshot.get("task", {})
        return isinstance(task, dict) and bool(task.get("exists"))
    units = snapshot.get("units", {})
    return isinstance(units, dict) and any(units.values())


def apply_scheduled_modes(
    previous: BackerConfig, desired: BackerConfig, *, enable_local_schedule: bool, headless: bool = False
) -> ModeApplyResult:
    """Apply local scheduling with full rollback of files, secrets, and real scheduler state."""
    rollback_errors: list[str] = []
    # `desired` is mutated in place (rescope), so the failure result must report an untouched copy.
    previous = previous.model_copy(deep=True)
    try:
        from backer.client.windows_service import (
            create_local_scheduled_task,
            create_local_systemd_timer,
            prepare_local_scheduler_mutation,
            remove_local_scheduled_task,
            remove_local_systemd_timer,
            restore_local_scheduler,
            restore_local_scheduler_trigger,
            snapshot_local_scheduler,
            verify_local_scheduler_frozen,
        )

        user_path = get_config_dir() / "config.yaml"
        machine_path = get_machine_config_dir() / "config.yaml"
        snapshots = {path: path.read_bytes() if path.exists() else None for path in (user_path, machine_path)}
        refs = {
            ref
            for record in previous.repositories.values()
            for ref in (record.passphrase_ref, record.storage_password_ref)
            if ref
        }
        machine_secrets = {ref: keystore.get(ref, machine_scope=True) for ref in refs}
        scheduler = snapshot_local_scheduler()
        mutation_started = False

        def refuse(freeze):
            detail = freeze.message
            if freeze.restore_failed:
                restored, message = restore_local_scheduler_trigger(scheduler)
                detail += "; trigger restored on retry" if restored else "; rollback failed: scheduler: " + message
            if mutation_started:
                errors = []
                for reference, value in machine_secrets.items():
                    try:
                        # Restore in place: only entries this call created are deleted.
                        if value is None:
                            keystore.delete(reference, machine_scope=True)
                        else:
                            keystore.put(reference, value, machine_scope=True)
                    except Exception as error:
                        errors.append(f"secret {reference}: {error}")
                for path, content in snapshots.items():
                    try:
                        _restore_config_file(path, content)
                    except Exception as error:
                        errors.append(f"config {path.name}: {error}")
                if errors:
                    detail += "; rollback failed: " + "; ".join(errors)
            return ModeApplyResult(False, previous, detail)

        freeze = prepare_local_scheduler_mutation(scheduler)
        if not freeze.ready:
            return refuse(freeze)

        if enable_local_schedule:
            if blocker := unattended_blocker(desired):
                raise ValueError(blocker)
            from backer.serverless.repositories import rescope_secrets_for_system

            freeze = verify_local_scheduler_frozen(scheduler)
            if not freeze.ready:
                return refuse(freeze)
            mutation_started = True
            rescope_secrets_for_system(desired)
            desired.save(machine_path)
            freeze = verify_local_scheduler_frozen(scheduler)
            if not freeze.ready:
                return refuse(freeze)
            if sys.platform == "win32":
                ok, message = create_local_scheduled_task()
            else:
                ok, message = create_local_systemd_timer(headless=headless)
            if not ok:
                raise OSError(message)
        else:
            if sys.platform == "win32":
                task = scheduler.get("task", {})
                freeze = verify_local_scheduler_frozen(scheduler)
                if not freeze.ready:
                    return refuse(freeze)
                if isinstance(task, dict) and task.get("exists") and not remove_local_scheduled_task():
                    raise OSError("Could not remove the local scheduled task")
            else:
                units = scheduler.get("units", {})
                freeze = verify_local_scheduler_frozen(scheduler)
                if not freeze.ready:
                    return refuse(freeze)
                if isinstance(units, dict) and any(units.values()):
                    ok, message = remove_local_systemd_timer(headless=headless)
                    if not ok:
                        raise OSError(message)
        mutation_started = True
        desired.save(machine_path)
        desired.save(user_path)
        committed = BackerConfig.load(user_path)
        machine = BackerConfig.load(machine_path)
        if committed != desired or machine != desired:
            raise OSError("Settings readback did not match the requested modes")
        return ModeApplyResult(True, desired, "Scheduled modes saved")
    except Exception as error:
        # Every rollback is attempted so the caller gets one stable failure result, but a refusal
        # that mutated nothing must never touch a secret or a config file.
        if locals().get("mutation_started"):
            for reference, value in locals().get("machine_secrets", {}).items():
                try:
                    # Restore in place: only entries this call created are deleted.
                    if value is None:
                        keystore.delete(reference, machine_scope=True)
                    else:
                        keystore.put(reference, value, machine_scope=True)
                except Exception as rollback_error:
                    rollback_errors.append(f"secret {reference}: {rollback_error}")
            for path, content in locals().get("snapshots", {}).items():
                try:
                    _restore_config_file(path, content)
                except Exception as rollback_error:
                    rollback_errors.append(f"config {path.name}: {rollback_error}")
        if "scheduler" in locals():
            try:
                ok, message = restore_local_scheduler(scheduler)
                if not ok:
                    rollback_errors.append(f"scheduler: {message}")
            except Exception as rollback_error:
                rollback_errors.append(f"scheduler: {rollback_error}")
        detail = str(error)
        if rollback_errors:
            detail += "; rollback failed: " + "; ".join(rollback_errors)
        return ModeApplyResult(False, previous, detail)
