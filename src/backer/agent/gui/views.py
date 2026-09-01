"""Retained Tk views over the unified local configuration."""

from __future__ import annotations

import base64
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import tkinter as tk
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import ttk

from backer.core import keystore
from backer.core.config import BackerConfig, ClientConfig
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
        self.empty = ttk.Label(self, text="No local backup jobs yet. Add a repository to begin.", style="Muted.TLabel")
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
                if repository and (location := repository_location(repository)):
                    try:
                        records = RepositoryMetadata(location).get_job_runs(name, 1)
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
        self.local_mode = tk.BooleanVar(value=False)
        self.server_mode = tk.BooleanVar(value=bool(app.config.server))
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
        ttk.Button(self, text="Manage repositories", command=lambda: app._show("home")).pack(anchor=tk.W, pady=(6, 0))
        ttk.Button(self, text="Save settings", command=self._save).pack(anchor=tk.W, pady=6)
        ttk.Button(self, text="Back", command=lambda: app._show("home")).pack(anchor=tk.W)

    def _save(self):
        url = self.server.get().strip()
        if url:
            current = self.app.config.server or ClientConfig()
            self.app.config.server = current.model_copy(update={"server_url": url})
        else:
            self.app.config.server = None
        save_config(self.app.config)
        self.app.set_status("Settings saved")
        self.app.apply_theme(self.mode.get())

    def _unattended(self):
        if blocker := unattended_blocker(self.app.config):
            self.app.set_status(blocker, error=True)
            return
        try:
            from backer.serverless.repositories import rescope_secrets_for_system

            rescope_secrets_for_system(self.app.config)
            self.app.config.save(get_machine_config_dir() / "config.yaml")
        except Exception as error:
            self.app.set_status(f"Could not prepare machine credentials: {error}", error=True)
            return
        if sys.platform == "win32":
            from backer.client.windows_service import create_local_scheduled_task

            ok, message = create_local_scheduled_task()
        else:
            from backer.client.windows_service import create_local_systemd_timer

            ok, message = create_local_systemd_timer()
        self.app.set_status(message, error=not ok)

    def _local_mode_changed(self):
        if self.local_mode.get():
            self._unattended()

    def _server_mode_changed(self):
        if self.server_mode.get():
            self.start_agent()
        elif self.service:
            self.start_agent()

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

    def connect(self):
        url, token = self.server.get().strip(), self.enrollment.get()
        if not url:
            self.app.set_status("Enter a server address", error=True)
            self.primary.focus_set()
            return

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
        url = self.server.get().strip()

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
        if self.service:
            self.service.stop()
            self.service = None
        self.app.config.server = None
        save_config(self.app.config)
        self.server.set("")
        self._disconnect_armed = False
        self.app.set_status("Server disconnected")

    def start_agent(self):
        server = self.app.config.server
        if not server or not server.client_id:
            self.app.set_status("Connect to a server first", error=True)
            return
        if self.service:
            self.service.stop()
            self.service = None
            self.agent_button.configure(text="Start agent")
            self.app.set_status("Server agent stopped")
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

    def service_status(self):
        from backer.client.windows_service import get_service_status

        status = get_service_status()
        self.app.set_status("Server service: " + (status.get("method") or "not installed"))

    def install_server_service(self):
        server = self.app.config.server
        if not server or not server.client_id:
            self.app.set_status("Connect to a server first", error=True)
            return
        from backer.client.windows_service import create_background_scheduled_task

        self._worker("server-service", lambda: create_background_scheduled_task(server.server_url))

    def test_scheduled_run(self):
        if sys.platform == "win32":
            self._worker(
                "scheduled-test",
                lambda: (
                    subprocess.run(["schtasks", "/run", "/tn", "BackerLocalSchedule"], capture_output=True).returncode
                    == 0,
                    "Scheduled local backup task started",
                ),
            )
        else:
            self._worker(
                "scheduled-test",
                lambda: (
                    subprocess.run(
                        ["systemctl", "--user", "start", "backer-local.service"], capture_output=True
                    ).returncode
                    == 0,
                    "Scheduled local backup service started",
                ),
            )

    def open_logs(self):
        directory = get_data_dir() / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(directory)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(directory)])
        self.app.set_status(f"Logs: {directory}")

    def check_for_updates(self):
        release_url = "https://git.stockhome.com.au/stocky789/backer/releases"
        if sys.platform != "win32":
            webbrowser.open(release_url)
            self.app.set_status("Opened Backer releases")
            return

        def install():
            destination = get_config_dir() / "backer-agent-setup.exe"
            urllib.request.urlretrieve(release_url + "/download/release-main/backer-agent-setup.exe", destination)
            subprocess.Popen([str(destination), "/S"])
            return True, "Installer started; restart Backer when it completes"

        self._worker("update", install)
