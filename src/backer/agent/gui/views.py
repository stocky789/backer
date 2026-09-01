"""Retained Tk views over the unified local configuration."""

from __future__ import annotations

import base64
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from pathlib import Path
from tkinter import filedialog, ttk
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from backer.core import keystore
from backer.core.config import BackerConfig, ClientConfig, ScheduleConfig
from backer.core.config import load_config as _load_config
from backer.core.paths import get_config_dir, get_data_dir, get_machine_config_dir
from backer.core.repo_metadata import RepositoryMetadata
from backer.serverless.store import read_runs


def load_config() -> BackerConfig:
    """Phase 1 config wrapper retained for the desktop application."""
    return _load_config()


def save_config(config: BackerConfig) -> None:
    """Phase 1 config wrapper retained for the desktop application."""
    config.save(get_config_dir() / "config.yaml")


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


def settings_update(
    config: BackerConfig, value: str, *, local_scheduled_mode: bool, server_agent_mode: bool
) -> BackerConfig:
    """Return one durable settings update without discarding registered credentials."""
    server = config.server
    if value.strip():
        current = server or ClientConfig()
        server = current.model_copy(update={"server_url": normalize_server_url(value)})
    return config.model_copy(
        update={
            "server": server,
            "local_scheduled_mode": local_scheduled_mode,
            "server_agent_mode": server_agent_mode,
        }
    )


def wait_for_scheduled_attempt(
    previous_id: str | None, read, *, token: str | None = None, timeout: float = 65, sleep=time.sleep
) -> tuple[bool, str]:
    """Wait for the scheduled identity's next persisted attempt, never its launch acknowledgement."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        attempts = read()
        current = next(
            (
                item
                for item in attempts
                if item.get("run_id") != previous_id and (token is None or str(item.get("run_id", "")).endswith(token))
            ),
            None,
        )
        if current:
            if current.get("status") == "success":
                return True, "Scheduled run completed"
            return False, current.get("error_message") or "Scheduled run failed"
        sleep(1)
    return False, "Scheduled identity did not write an attempt record"


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


def prepare_scheduled_test(config: BackerConfig, job_name: str, token: str) -> tuple[Path, list[str]]:
    """Build a one-job machine-only config; task commands never receive user-controlled names."""
    from backer.serverless.scheduled_test import test_directory

    job = config.jobs.get(job_name)
    if not job or not (record := config.repositories.get(job.repository)):
        raise ValueError("Selected job is not configured")
    directory = test_directory(token)
    if directory.exists():
        raise ValueError("Scheduled test context already exists")
    clone = config.model_copy(deep=True)
    clone.jobs = {job_name: job.model_copy(update={"schedule": ScheduleConfig(cron="* * * * *")})}
    clone.repositories = {job.repository: record}
    refs = []
    try:
        copied = {}
        for field in ("passphrase_ref", "storage_password_ref"):
            reference = getattr(record, field)
            if not reference:
                continue
            value = keystore.get(reference) or keystore.get(reference, machine_scope=True)
            if value is None:
                raise ValueError(f"Repository '{record.name}' secret cannot be read")
            temporary = f"backer/scheduled-test/{token}/{field}"
            keystore.put(temporary, value, machine_scope=True)
            refs.append(temporary)
            copied[field] = temporary
        clone.repositories[job.repository] = record.model_copy(update={**copied, "scope": "machine"})
        directory.mkdir(parents=True)
        clone.save(directory / "config.yaml")
        return directory, refs
    except Exception as error:
        cleanup_errors = []
        for reference in refs:
            try:
                keystore.delete(reference, machine_scope=True)
            except Exception as cleanup_error:
                cleanup_errors.append(str(cleanup_error))
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            cleanup_errors.append(str(cleanup_error))
        if cleanup_errors:
            raise RuntimeError(f"{error}; cleanup failed: {'; '.join(cleanup_errors)}") from error
        raise


def remove_scheduled_test(directory: Path, refs: list[str]) -> list[str]:
    errors = []
    for reference in refs:
        try:
            keystore.delete(reference, machine_scope=True)
        except Exception as error:
            errors.append(str(error))
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        pass
    except OSError as error:
        errors.append(str(error))
    return errors


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


class HomeView(ttk.Frame):
    primary = None

    def __init__(self, app):
        super().__init__(app.container, padding=18)
        self.app = app
        ttk.Label(self, text="Local backups", font=("Segoe UI", 16, "bold")).pack(anchor=tk.W)
        self.server_strip = ttk.Label(self, text="Server-managed jobs are not listed here.", style="Muted.TLabel")
        if app.config.server:
            self.server_strip.pack(anchor=tk.W, pady=(4, 8))
        self.tree = ttk.Treeview(
            self, columns=("job", "source", "repository", "schedule", "last", "size"), show="headings"
        )
        for column, label, width in (
            ("job", "Job", 130),
            ("source", "Source", 220),
            ("repository", "Repository", 150),
            ("schedule", "Schedule", 120),
            ("last", "Last run", 140),
            ("size", "Size", 90),
        ):
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, anchor=tk.W)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.bind("<Double-1>", lambda _event: self.app.backup_selected())
        buttons = ttk.Frame(self)
        buttons.pack(fill=tk.X, pady=(10, 0))
        self.add = ttk.Button(buttons, text="Add repository", command=lambda: app._show("repository"))
        self.add.pack(side=tk.LEFT)
        self.backup = ttk.Button(buttons, text="Back up now", command=app.backup_selected, state=tk.DISABLED)
        self.backup.pack(side=tk.LEFT, padx=6)
        self.restore = ttk.Button(buttons, text="Restore", command=app.restore_selected, state=tk.DISABLED)
        self.restore.pack(side=tk.LEFT)
        self.edit = ttk.Button(buttons, text="Edit", command=lambda: app._show("repository"), state=tk.DISABLED)
        self.edit.pack(side=tk.LEFT, padx=6)
        self.remove = ttk.Button(buttons, text="Remove", command=app.remove_selected_job, state=tk.DISABLED)
        self.remove.pack(side=tk.RIGHT)
        self.tree.bind("<<TreeviewSelect>>", self._selection)
        self.empty = ttk.Frame(self)
        ttk.Label(self.empty, text="No local backup jobs yet.", style="Muted.TLabel").pack()
        ttk.Button(self.empty, text="Add repository", command=lambda: app._show("repository")).pack(pady=(8, 0))
        self.primary = self.add

    def _selection(self, _event=None):
        state = tk.NORMAL if self.tree.selection() else tk.DISABLED
        for button in (self.backup, self.restore, self.edit, self.remove):
            button.configure(state=state)

    def refresh(self):
        self.empty.place_forget()
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.delete(*self.tree.get_children())
        for name, job in self.app.config.jobs.items():
            repository = self.app.config.repositories.get(job.repository)
            schedule = job.schedule.cron if job.schedule and job.schedule.cron else "Manual"
            self.tree.insert(
                "",
                tk.END,
                iid=name,
                values=(name, job.source.path, repository.name if repository else "Missing", schedule, "…", "…"),
            )
        if not self.app.config.jobs:
            self.tree.pack_forget()
            self.empty.place(relx=0.5, rely=0.45, anchor=tk.CENTER)
        self._selection()
        self._refresh_runs()

    def _refresh_runs(self):
        generation = self.app.generation("home")

        def worker():
            rows = {}
            for name, job in self.app.config.jobs.items():
                runs = read_runs(get_data_dir(), name, 1)
                repository = self.app.config.repositories.get(job.repository)
                remote = None
                if repository:
                    try:
                        records = repository_history(repository, name)
                        remote = records[0] if records else None
                    except Exception:
                        remote = None
                rows[name] = run_summary(runs[0] if runs else None, remote)
            self.app.marshal(generation, lambda: self._apply_runs(generation, rows), lambda: self.app.visible == "home")

        threading.Thread(target=worker, daemon=True).start()
        try:
            self.app.root.after(
                60000, lambda: self._refresh_runs() if self.app.alive and self.app.visible == "home" else None
            )
        except (RuntimeError, tk.TclError):
            pass

    def _apply_runs(self, generation, rows):
        if not self.app.current(generation):
            return
        for name, (status, size) in rows.items():
            if self.tree.exists(name):
                values = list(self.tree.item(name, "values"))
                values[-2:] = (status, size)
                self.tree.item(name, values=values)


class SimpleView(ttk.Frame):
    def __init__(self, app, title, body, primary_text=None, primary=None):
        super().__init__(app.container, padding=18)
        self.app = app
        ttk.Label(self, text=title, font=("Segoe UI", 16, "bold")).pack(anchor=tk.W)
        ttk.Label(self, text=body, style="Muted.TLabel", wraplength=650).pack(anchor=tk.W, pady=(8, 16))
        self.primary = ttk.Button(self, text=primary_text, command=primary) if primary_text else None
        if self.primary:
            self.primary.pack(anchor=tk.W)
        ttk.Button(self, text="Back", command=lambda: app._show("home")).pack(anchor=tk.W, pady=(16, 0))


class SettingsView(ttk.Frame):
    primary = None

    def __init__(self, app):
        super().__init__(app.container, padding=18)
        self.app = app
        self.service = None
        self._disconnect_armed = False
        ttk.Label(self, text="Settings", font=("Segoe UI", 16, "bold")).pack(anchor=tk.W)
        self.server = tk.StringVar(value=app.config.server.server_url if app.config.server else "")
        self.enrollment = tk.StringVar()
        ttk.Label(self, text="Server address").pack(anchor=tk.W, pady=(14, 0))
        self.primary = ttk.Entry(self, textvariable=self.server, width=58)
        self.primary.pack(fill=tk.X)
        ttk.Label(self, text="Enrollment token", style="Muted.TLabel").pack(anchor=tk.W, pady=(6, 0))
        ttk.Entry(self, textvariable=self.enrollment, show="*").pack(fill=tk.X)
        server_actions = ttk.Frame(self)
        server_actions.pack(anchor=tk.W, pady=(6, 0))
        ttk.Button(server_actions, text="Connect", command=self.connect).pack(side=tk.LEFT)
        ttk.Button(server_actions, text="Check status", command=self.check_connection).pack(side=tk.LEFT, padx=6)
        ttk.Button(server_actions, text="Disconnect", command=self.disconnect).pack(side=tk.LEFT)
        self.agent_button = ttk.Button(server_actions, text="Start agent", command=self.start_agent)
        self.agent_button.pack(side=tk.LEFT, padx=6)
        self.mode = tk.StringVar(value="system")
        self.local_mode = tk.BooleanVar(value=app.config.local_scheduled_mode)
        self.server_mode = tk.BooleanVar(value=app.config.server_agent_mode)
        ttk.Label(self, text="Appearance").pack(anchor=tk.W, pady=(14, 0))
        ttk.Combobox(self, textvariable=self.mode, values=("system", "light", "dark"), state="readonly").pack(
            anchor=tk.W
        )
        self.unattended = ttk.Button(self, text="Set up unattended scheduled backups", command=self._unattended)
        self.unattended.pack(anchor=tk.W, pady=(14, 0))
        modes = ttk.Frame(self)
        modes.pack(anchor=tk.W, pady=(6, 0))
        ttk.Checkbutton(
            modes, text="Local scheduled mode", variable=self.local_mode, command=self._local_mode_changed
        ).pack(side=tk.LEFT)
        ttk.Checkbutton(
            modes, text="Server agent mode", variable=self.server_mode, command=self._server_mode_changed
        ).pack(side=tk.LEFT, padx=12)
        scheduled = ttk.Frame(self)
        scheduled.pack(anchor=tk.W, pady=(6, 0))
        self.scheduled_job = tk.StringVar(value=next(iter(app.config.jobs), ""))
        ttk.Combobox(
            scheduled, textvariable=self.scheduled_job, values=tuple(app.config.jobs), state="readonly", width=24
        ).pack(side=tk.LEFT)
        ttk.Button(scheduled, text="Test scheduled run now", command=self.test_scheduled_run).pack(side=tk.LEFT)
        ttk.Button(scheduled, text="Server service status", command=self.service_status).pack(side=tk.LEFT, padx=6)
        ttk.Button(scheduled, text="Install server agent service", command=self.install_server_service).pack(
            side=tk.LEFT
        )
        utilities = ttk.Frame(self)
        utilities.pack(anchor=tk.W, pady=(14, 0))
        ttk.Button(utilities, text="Open logs", command=self.open_logs).pack(side=tk.LEFT)
        ttk.Button(utilities, text="Check for updates", command=self.check_for_updates).pack(side=tk.LEFT, padx=6)
        backend = keystore.backend_name()
        ttk.Label(self, text=f"Repository secrets use {backend}.", style="Muted.TLabel").pack(
            anchor=tk.W, pady=(10, 0)
        )
        self.repository_choice = tk.StringVar(value=next(iter(app.config.repositories), ""))
        self.repository_detail = tk.StringVar(value=repository_details(app.config, self.repository_choice.get()))
        repository_actions = ttk.Frame(self)
        repository_actions.pack(fill=tk.X, pady=(6, 0))
        ttk.Combobox(
            repository_actions,
            textvariable=self.repository_choice,
            values=tuple(app.config.repositories),
            state="readonly",
            width=24,
        ).pack(side=tk.LEFT)
        ttk.Button(repository_actions, text="Show passphrase", command=self.reveal_passphrase).pack(
            side=tk.LEFT, padx=6
        )
        ttk.Button(repository_actions, text="Save recovery record", command=self.save_recovery_record).pack(
            side=tk.LEFT
        )
        ttk.Label(self, textvariable=self.repository_detail, style="Muted.TLabel", wraplength=650).pack(anchor=tk.W)
        self.repository_choice.trace_add("write", lambda *_args: self._show_repository_details())
        self._recovery_target = None
        self._recovery_ack = tk.BooleanVar(value=False)
        self._recovery_warning = tk.StringVar()
        self._recovery_frame = ttk.Frame(self)
        ttk.Label(
            self._recovery_frame, textvariable=self._recovery_warning, style="Danger.TLabel", wraplength=650
        ).pack(anchor=tk.W)
        ttk.Checkbutton(
            self._recovery_frame,
            text="I understand this file contains my passphrase in plain text",
            variable=self._recovery_ack,
            command=self._update_recovery_save_state,
        ).pack(anchor=tk.W, pady=(6, 0))
        self._recovery_save_button = ttk.Button(
            self._recovery_frame, text="Save recovery record", command=self._save_recovery_record
        )
        self._recovery_save_button.pack(anchor=tk.W, pady=(6, 0))
        self._recovery_save_button.configure(state=tk.DISABLED)
        ttk.Button(self, text="Save settings", command=self._save).pack(anchor=tk.W, pady=6)
        ttk.Button(self, text="Back", command=lambda: app._show("home")).pack(anchor=tk.W)

    def _save(self):
        previous = self.app.config
        try:
            updated = settings_update(
                previous,
                self.server.get(),
                local_scheduled_mode=self.local_mode.get(),
                server_agent_mode=self.server_mode.get(),
            )
        except ValueError as error:
            self.app.set_status(f"Settings were not saved: {error}", error=True)
            return
        self._apply_modes(previous, updated)

    def _unattended(self):
        self.local_mode.set(True)
        self._save()

    def _local_mode_changed(self):
        self.app.set_status("Save settings to apply scheduled modes")

    def _server_mode_changed(self):
        self.app.set_status("Save settings to apply scheduled modes")

    def _apply_modes(self, previous: BackerConfig, desired: BackerConfig):

        def apply():
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
            changed_task = False

            def restore_file(path, content):
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)

            def restore_task():
                if sys.platform == "win32":
                    from backer.client.windows_service import create_local_scheduled_task, remove_local_scheduled_task
                else:
                    from backer.client.windows_service import create_local_systemd_timer, remove_local_systemd_timer
                if previous.local_scheduled_mode:
                    return create_local_scheduled_task() if sys.platform == "win32" else create_local_systemd_timer()
                remove_local_scheduled_task() if sys.platform == "win32" else remove_local_systemd_timer()
                return True, ""

            try:
                if desired.local_scheduled_mode:
                    if blocker := unattended_blocker(desired):
                        raise ValueError(blocker)
                    from backer.serverless.repositories import rescope_secrets_for_system

                    rescope_secrets_for_system(desired)
                    desired.save(machine_path)
                    if sys.platform == "win32":
                        from backer.client.windows_service import create_local_scheduled_task

                        ok, message = create_local_scheduled_task()
                    else:
                        from backer.client.windows_service import create_local_systemd_timer

                        ok, message = create_local_systemd_timer()
                    changed_task = True
                    if not ok:
                        raise OSError(message)
                elif previous.local_scheduled_mode:
                    if sys.platform == "win32":
                        from backer.client.windows_service import remove_local_scheduled_task

                        if not remove_local_scheduled_task():
                            raise OSError("Could not remove the local scheduled task")
                    else:
                        from backer.client.windows_service import remove_local_systemd_timer

                        remove_local_systemd_timer()
                    changed_task = True
                desired.save(machine_path)
                desired.save(user_path)
                committed = BackerConfig.load(user_path)
                if (
                    committed.local_scheduled_mode != desired.local_scheduled_mode
                    or committed.server_agent_mode != desired.server_agent_mode
                ):
                    raise OSError("Settings readback did not match the requested modes")
                machine = BackerConfig.load(machine_path)
                if machine.local_scheduled_mode != desired.local_scheduled_mode:
                    raise OSError("Machine settings readback did not match local mode")
                return True, desired, "Scheduled modes saved"
            except Exception as error:
                for reference, value in machine_secrets.items():
                    keystore.delete(reference, machine_scope=True)
                    if value is not None:
                        keystore.put(reference, value, machine_scope=True)
                for path, content in snapshots.items():
                    restore_file(path, content)
                if changed_task:
                    restore_task()
                return False, previous, str(error)

        def guarded_apply():
            try:
                return apply()
            except Exception as error:
                return False, previous, str(error)

        self._worker("apply-modes", guarded_apply, self._modes_applied)

    def _modes_applied(self, result):
        ok, config, message = result
        self.app.config = config
        self.local_mode.set(config.local_scheduled_mode)
        self.server_mode.set(config.server_agent_mode)
        if ok:
            self.app.apply_theme(self.mode.get())
        self.app.set_status(message, error=not ok)

    def _worker(self, name, work, done=None):
        token = self.app.generation("settings")

        def run():
            try:
                result = work()
            except Exception as error:
                result = (False, str(error))
            self.app.marshal(
                token,
                lambda: done(result) if done else self.app.set_status(result[1], error=not result[0]),
                lambda: self.app.visible == "settings",
            )

        threading.Thread(target=run, name=f"backer-{name}", daemon=True).start()

    def _server_status(self, status):
        token = ("settings", self.app._generations.get("settings", 0))
        self.app.marshal(token, lambda: self.app.set_status(status), lambda: self.app.visible == "settings")

    def _show_repository_details(self):
        self.repository_detail.set(repository_details(self.app.config, self.repository_choice.get()))

    def _repository_passphrase(self) -> tuple[str | None, str]:
        record = self.app.config.repositories.get(self.repository_choice.get())
        if not record or not record.passphrase_ref:
            return None, "Repository passphrase is unavailable"
        value = keystore.get(record.passphrase_ref) or keystore.get(record.passphrase_ref, machine_scope=True)
        return value, "" if value else "Repository passphrase is unavailable"

    def reveal_passphrase(self):
        if not self.app.confirm_reveal_passphrase("Reveal this repository passphrase on screen?"):
            return

        def reveal():
            value, message = self._repository_passphrase()
            return bool(value), value or message

        self._worker("reveal-passphrase", reveal, self._revealed_passphrase)

    def _revealed_passphrase(self, result):
        if result[0]:
            self.repository_detail.set(self.repository_detail.get().split(" · passphrase")[0] + " · " + result[1])
        self.app.set_status("Passphrase revealed" if result[0] else result[1], error=not result[0])

    def save_recovery_record(self):
        record = self.app.config.repositories.get(self.repository_choice.get())
        if not record:
            self.app.set_status("Select a repository", error=True)
            return
        target = filedialog.asksaveasfilename(title="Save recovery record", defaultextension=".txt")
        if not target:
            return
        self._recovery_target = Path(target)
        self._recovery_ack.set(False)
        self._recovery_warning.set(
            f"This writes your passphrase to {self._recovery_target} in plain text. "
            "A copy saved on this computer will not help if this computer fails."
        )
        self._recovery_frame.pack(fill=tk.X, pady=(6, 0))
        self._update_recovery_save_state()

    def _update_recovery_save_state(self):
        ready = self._recovery_target and self._recovery_ack.get()
        self._recovery_save_button.configure(state=tk.NORMAL if ready else tk.DISABLED)

    def _save_recovery_record(self):
        record = self.app.config.repositories.get(self.repository_choice.get())
        target = self._recovery_target
        if not record or not target or not self._recovery_ack.get():
            self.app.set_status("Choose a recovery record location and acknowledge the plaintext warning", error=True)
            return

        def save():
            from backer.agent.gui.wizard import RepositoryWizard, recovery_record

            value, message = self._repository_passphrase()
            if not value:
                return False, message
            location = RepositoryWizard._recovery_location(record)
            command, instruction = RepositoryWizard._recovery_command(record, location)
            target.write_text(
                recovery_record(
                    record.name, location, value, connect_command=command, credential_instruction=instruction
                ),
                encoding="utf-8",
            )
            if os.name != "nt":
                target.chmod(0o600)
            return True, "Recovery record saved; keep it off this computer"

        self._worker("recovery-record", save)

    def connect(self):
        try:
            url = normalize_server_url(self.server.get())
        except ValueError as error:
            self.app.set_status(str(error), error=True)
            self.primary.focus_set()
            return
        self.server.set(url)
        token = self.enrollment.get()

        def connect():
            from backer._version import __version__

            health = urllib.request.Request(f"{url.rstrip('/')}/health", method="GET")
            health.add_header("User-Agent", f"Backer-Agent/{__version__}")
            with urllib.request.urlopen(health, timeout=10) as response:
                if response.status != 200:
                    raise RuntimeError(f"Server returned status {response.status}")
            payload = json.dumps(
                {
                    "hostname": socket.gethostname(),
                    "version": __version__,
                    "os_info": f"{platform.system()} {platform.release()}",
                    "enrollment_token": token,
                }
            ).encode()
            request = urllib.request.Request(f"{url.rstrip('/')}/api/v1/clients/register", data=payload, method="POST")
            request.add_header("Content-Type", "application/json")
            request.add_header("User-Agent", f"Backer-Agent/{__version__}")
            current = self.app.config.server
            if current and current.client_id and current.client_secret:
                credentials = base64.b64encode(f"{current.client_id}:{current.client_secret}".encode()).decode()
                request.add_header("Authorization", f"Basic {credentials}")
            with urllib.request.urlopen(request, timeout=10) as response:
                result = json.loads(response.read().decode())
            self.app.config.server = ClientConfig(
                server_url=url,
                client_id=result.get("client_id", current.client_id if current else ""),
                client_secret=result.get("client_secret", current.client_secret if current else ""),
            )
            save_config(self.app.config)
            return True, f"Connected as {socket.gethostname()}"

        self._worker("connect", connect)

    def check_connection(self):
        try:
            url = normalize_server_url(self.server.get())
        except ValueError as error:
            self.app.set_status(str(error), error=True)
            return
        self.server.set(url)

        def check():
            request = urllib.request.Request(f"{url.rstrip('/')}/health", method="GET")
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status != 200:
                    raise RuntimeError(f"Server returned status {response.status}")
            return True, "Server connected"

        self._worker("health", check)

    def disconnect(self):
        if not self._disconnect_armed:
            self._disconnect_armed = True
            self.app.set_status("Click Disconnect again to remove the saved server credentials")
            return
        service, current = self.service, self.app.config

        def disconnect():
            if service:
                service.stop()
            updated = current.model_copy(update={"server": None, "server_agent_mode": False})
            save_config(updated)
            return True, updated

        self._worker("disconnect", disconnect, self._disconnected)

    def _disconnected(self, result):
        if result[0]:
            self.service = None
            self.app.config = result[1]
            self.server.set("")
            self.server_mode.set(False)
            self._disconnect_armed = False
            self.app.set_status("Server disconnected")
            return
        self.app.set_status(result[1], error=True)

    def start_agent(self):
        server = self.app.config.server
        if not server or not server.client_id:
            self.app.set_status("Connect to a server first", error=True)
            return
        if self.service:
            service = self.service

            def stop():
                service.stop()
                return True, "Server agent stopped"

            self._worker("stop-agent", stop, self._agent_stopped)
            return

        def start():
            from backer.agent.service import AgentService

            service = AgentService(
                server_url=server.server_url,
                client_id=server.client_id,
                client_secret=server.client_secret,
                status_callback=self._server_status,
            )
            service.start()
            self.service = service
            return True, "Server agent running"

        self._worker("agent", start, lambda result: self._agent_started(result))

    def _agent_started(self, result):
        self.app.set_status(result[1], error=not result[0])
        if result[0]:
            self.agent_button.configure(text="Stop agent")

    def _agent_stopped(self, result):
        self.app.set_status(result[1], error=not result[0])
        if result[0]:
            self.service = None
            self.agent_button.configure(text="Start agent")

    def service_status(self):
        def probe():
            from backer.client.windows_service import get_service_status

            status = get_service_status()
            return True, "Server service: " + (status.get("method") or "not installed")

        self._worker("service-status", probe)

    def install_server_service(self):
        server = self.app.config.server
        if not server or not server.client_id:
            self.app.set_status("Connect to a server first", error=True)
            return
        from backer.client.windows_service import create_background_scheduled_task

        self._worker("server-service", lambda: create_background_scheduled_task(server.server_url))

    def test_scheduled_run(self):
        name = self.scheduled_job.get()
        if not name or name not in self.app.config.jobs:
            self.app.set_status("Select a configured local job", error=True)
            return

        def run():
            if blocker := unattended_blocker(self.app.config):
                return False, blocker
            token = uuid4().hex[:12]
            directory, refs = prepare_scheduled_test(self.app.config, name, token)
            data_dir = directory / "data"
            before = read_runs(data_dir, name, 1)
            previous = before[0].run_id if before else None
            if sys.platform == "win32":
                from backer.client.windows_service import (
                    create_local_scheduled_test_task,
                    remove_local_scheduled_test_task,
                )

                launch, message = create_local_scheduled_test_task(token)

                def cleanup():
                    return remove_local_scheduled_test_task(token)

            else:
                from backer.client.windows_service import (
                    create_local_systemd_test_service,
                    remove_local_systemd_test_service,
                )

                launch, message = create_local_systemd_test_service(token)

                def cleanup():
                    return remove_local_systemd_test_service(token)
            if not launch:
                cleanup_errors = remove_scheduled_test(directory, refs)
                return False, message + ("; cleanup: " + "; ".join(cleanup_errors) if cleanup_errors else "")
            try:
                return wait_for_scheduled_attempt(
                    previous,
                    lambda: [item.to_dict() for item in read_runs(data_dir, name, 2)],
                    token=token,
                )
            finally:
                stopped, cleanup_message = cleanup()
                cleanup_errors = remove_scheduled_test(directory, refs)
                if not stopped or cleanup_errors:
                    raise RuntimeError(cleanup_message + ("; " + "; ".join(cleanup_errors) if cleanup_errors else ""))

        self._worker("scheduled-test", run)

    def open_logs(self):
        def open_directory():
            directory = get_data_dir() / "logs"
            directory.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(directory)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(directory)])
            return True, f"Logs: {directory}"

        self._worker("open-logs", open_directory)

    def check_for_updates(self):
        release_url = "https://git.stockhome.com.au/stocky789/backer/releases"
        def install():
            if sys.platform == "win32":
                destination = get_config_dir() / "backer-agent-setup.exe"
                urllib.request.urlretrieve(release_url + "/download/release-main/backer-agent-setup.exe", destination)
                subprocess.Popen([str(destination), "/S"])
                return True, "Installer started; restart Backer when it completes"
            repository = release_url.removesuffix("/releases")
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", f"git+{repository}.git@main"],
                capture_output=True,
                text=True,
            )
            if result.returncode:
                return False, result.stderr.strip() or f"Update failed; releases: {release_url}"
            return True, "Update complete; restart Backer"

        self._worker("update", install)
