"""Backer's single-window desktop companion for local and server-managed backups."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from backer.agent.gui import theme
from backer.agent.gui.views import (
    HomeView,
    SettingsView,
    SimpleView,
    load_config,
    save_config,
    save_schedule_pause,
    supported_repository_types,
)
from backer.agent.gui.wizard import RepositoryWizard
from backer.core.messages import explain_failure
from backer.core.paths import get_data_dir
from backer.serverless.runs import run_local_job
from backer.serverless.schedule import scheduling_paused

REPOSITORY_URL = "https://git.stockhome.com.au/stocky789/backer"
INSTALLER_URL = f"{REPOSITORY_URL}/releases/download/release-main/backer-agent-setup.exe"
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
LOG_DIR = get_data_dir() / "logs"
LOG_FILE: Path | None = None
try:
    import pystray
    from PIL import Image

    TRAY_AVAILABLE = sys.platform == "win32"
except ImportError:
    TRAY_AVAILABLE = False


def notification_allowed(state: dict, name: str, report: dict, today: str) -> bool:
    """Keep backup notifications useful rather than a nightly source of spam."""
    if report.get("cancelled"):
        return False
    if report.get("needs_input"):
        return state.get("input", {}).get(name) != report.get("run_id")
    if report.get("success"):
        return name not in state.get("first_success", [])
    return state.get("failure_day", {}).get(name) != today


class BackerAgentApp:
    """Retained-view Tk shell; unified config and runner stay authoritative."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Backer")
        self.root.geometry("760x560")
        self.root.minsize(700, 480)
        icon = APP_DIR / "backer.ico"
        if icon.exists():
            self.root.iconbitmap(str(icon))
        self.config = load_config()
        self.visible = ""
        self.views = {}
        self._generations = {}
        self.running = False
        self.alive = True
        self.progress_frame = None
        self.progress_at = 0.0
        self.run_cancel = threading.Event()
        self.progress_log = deque(maxlen=500)
        self.tray_icon = None
        self._notification_run = None
        self._notification_state = self._read_notification_state()
        self.container = ttk.Frame(self.root)
        self.container.pack(fill=tk.BOTH, expand=True)
        self.status_var = tk.StringVar(value="OK · Ready")
        self.pause_var = tk.StringVar(value="")
        self.subtitle_var = tk.StringVar(value="Local backups")
        self._setup_menu()
        self._setup_status()
        self.apply_theme()
        self._make_tray()
        self.root.protocol("WM_DELETE_WINDOW", self.on_window_close)
        self._show("welcome" if not self.config.repositories and not self.config.server else "home")
        self.root.after(0, self._replay_pending_notifications)

    def _setup_menu(self):
        menu = tk.Menu(self.root)
        self.root.config(menu=menu)
        hub = tk.Menu(menu, tearoff=0)
        menu.add_cascade(label="Backer", menu=hub)
        hub.add_command(label="Home", command=lambda: self._show("home"))
        hub.add_command(label="Add repository", command=lambda: self._show("repository"))
        hub.add_command(label="Settings", command=lambda: self._show("settings"))
        hub.add_separator()
        hub.add_command(label="Exit", command=self.on_exit)

    def _setup_status(self):
        strip = ttk.Frame(self.root, padding=(12, 7))
        strip.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Label(strip, textvariable=self.status_var, style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Label(strip, textvariable=self.pause_var, style="Muted.TLabel").pack(side=tk.LEFT, padx=12)
        ttk.Label(strip, textvariable=self.subtitle_var, style="Muted.TLabel").pack(side=tk.RIGHT)

    def apply_theme(self, override=None):
        self.tokens = theme.apply(ttk.Style(self.root), override)

    def generation(self, name):
        self._generations[name] = self._generations.get(name, 0) + 1
        return name, self._generations[name]

    def current(self, token):
        return self._generations.get(token[0]) == token[1]

    def marshal(self, token, callback, current=None):
        """Post worker results only while this view and root still exist."""
        if not self.alive:
            return

        def guarded():
            if self.alive and self.current(token) and (current is None or current()):
                callback()

        try:
            self.root.after(0, guarded)
        except (RuntimeError, tk.TclError):
            pass

    def _build(self, name):
        if name == "welcome":
            return SimpleView(
                self,
                "Choose how Backer runs",
                "Set up local backups or join a server. Both can coexist.",
                "Set up local backups",
                lambda: self._show("repository"),
            )
        if name == "home":
            return HomeView(self)
        if name == "repository":
            if not supported_repository_types():
                return SimpleView(self, "Local backups unavailable", "No tested storage path is available here.")
            return RepositoryWizard(self)
        if name == "settings":
            return SettingsView(self)
        if name == "run":
            return RunView(self)
        if name == "restore":
            return RestoreView(self)
        return SimpleView(self, name.title(), "This view is unavailable until its command has a passing platform test.")

    def _show(self, name):
        """Show exactly one retained child; Escape returns to Home."""
        if self.visible:
            self.generation(self.visible)
            self.views[self.visible].pack_forget()
        view = self.views.get(name)
        if view is None:
            view = self._build(name)
            self.views[name] = view
        self.visible = name
        self.generation(name)
        if hasattr(view, "show"):
            view.show()
        if name == "home":
            view.refresh()
        view.pack(fill=tk.BOTH, expand=True)
        self.subtitle_var.set("Local backups" if name == "home" else name.replace("_", " ").title())
        self.root.bind_all("<Escape>", lambda _event: self._show("home") if self.visible != "home" else None)
        self.root.bind(
            "<Return>",
            lambda _event: (
                view.primary.invoke()
                if getattr(view, "primary", None) and str(view.primary.cget("state")) != "disabled"
                else None
            ),
        )
        if getattr(view, "primary", None):
            view.primary.focus_set()

    def set_status(self, message, *, error=False):
        self.status_var.set(("Failed" if error else "OK") + " · " + message)
        if error:
            self.progress_log.append(explain_failure(message))
            self.progress_log.append(message)
            logging.getLogger(__name__).error(message)

    def _notification_path(self):
        return get_data_dir() / "gui-notifications.json"

    def _read_notification_state(self):
        try:
            with self._notification_path().open(encoding="utf-8") as handle:
                state = json.load(handle)
            return state if isinstance(state, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_notification_state(self):
        path = self._notification_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._notification_state), encoding="utf-8")
        temporary.replace(path)

    def _record_run_notification(self, name, report):
        """Persist the intentionally small notification policy and attention count."""
        today = datetime.now(UTC).date().isoformat()
        if not notification_allowed(self._notification_state, name, report, today):
            return
        if report.get("needs_input"):
            self._notification_state.setdefault("input", {})[name] = report.get("run_id")
            title, body = "Backer needs input", f"{name} needs your attention."
        elif report.get("success"):
            self._notification_state.setdefault("first_success", []).append(name)
            self._notification_state.setdefault("attention", {}).pop(name, None)
            title, body = "Backer", f"{name} completed its first backup."
        else:
            self._notification_state.setdefault("failure_day", {})[name] = today
            self._notification_state.setdefault("attention", {})[name] = report.get("run_id") or "latest"
            self._notification_run = name
            title, body = "Backer", f"{name} backup did not run. Open Backer for details."
        self._save_notification_state()
        self._refresh_tray_menu()
        self.notify(title, body)

    def _replay_pending_notifications(self):
        """Surface a scheduled failure once when the desktop next starts."""
        from backer.serverless.store import read_runs

        for name in self.config.jobs:
            rows = read_runs(get_data_dir(), name, 1)
            if not rows:
                continue
            run = rows[0]
            self._record_run_notification(name, {
                "success": run.status.value == "success", "run_id": run.run_id,
                "needs_input": bool(run.error_message and "needs" in run.error_message.lower()),
            })

    def record_run_result(self, name, report):
        self._record_run_notification(name, report or {})

    def backup_job(self, name):
        if name not in self.config.jobs:
            return
        self._show("run")
        self.views["run"].start(name)

    def backup_selected(self):
        home = self.views.get("home")
        if home and home.tree.selection():
            self.backup_job(home.tree.selection()[0])

    def restore_selected(self):
        home = self.views.get("home")
        if home and home.tree.selection():
            self._show("restore")
            self.views["restore"].load(home.tree.selection()[0])

    def remove_selected_job(self):
        home = self.views.get("home")
        if home and home.tree.selection() and self.confirm_remove_job():
            self.config.jobs.pop(home.tree.selection()[0], None)
            save_config(self.config)
            home.refresh()
            self.set_status("Job removed")

    def confirm_restore_overwrite(self):
        return messagebox.askyesno("Restore over files", "Existing files may be replaced. Continue?", parent=self.root)

    def confirm_remove_job(self):
        return messagebox.askyesno("Remove job", "Remove this backup job?", parent=self.root)

    def confirm_remove_repository(self, connection=None):
        if connection:
            title = "Disconnect network share"
            prompt = (
                f"Disconnect the existing Windows connection to {connection}? "
                "Explorer windows using it may stop working."
            )
        else:
            title, prompt = "Remove repository", "Remove this repository configuration?"
        return messagebox.askyesno(title, prompt, parent=self.root)

    def confirm_quit_during_run(self):
        return messagebox.askyesno(
            "Quit during backup", "The running backup will be cancelled. Quit?", parent=self.root
        )

    def confirm_reveal_passphrase(self, warning=None):
        return messagebox.askyesno(
            "Reveal passphrase", warning or "Reveal the repository passphrase?", parent=self.root
        )

    def _make_tray(self):
        if not TRAY_AVAILABLE:
            return
        try:
            image = Image.open(str(APP_DIR / "backer.ico"))
            self.tray_icon = pystray.Icon("backer", image, "Backer")
            self._refresh_tray_menu()
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        except Exception:
            self.tray_icon = None

    def _refresh_tray_menu(self):
        paused = scheduling_paused(self.config, datetime.now(UTC))
        self.pause_var.set("Paused" if paused else "")
        if not self.tray_icon:
            return
        until = self.config.local_scheduled_pause_until
        pause_label = "Paused" if paused and until is None else (
            f"Paused until {until.astimezone().strftime('%H:%M')}" if paused and until else "Backups active"
        )
        jobs = tuple(
            pystray.MenuItem(name, lambda _icon, _item, job=name: self.root.after(0, lambda: self.backup_job(job)))
            for name in self.config.jobs
        )
        pause = pystray.Menu(
            pystray.MenuItem("For 1 hour", lambda *_: self.root.after(0, lambda: self.pause_backups("hour"))),
            pystray.MenuItem("Until tomorrow", lambda *_: self.root.after(0, lambda: self.pause_backups("tomorrow"))),
            pystray.MenuItem(
                "Until turned back on", lambda *_: self.root.after(0, lambda: self.pause_backups("forever"))
            ),
            pystray.MenuItem("Resume backups", lambda *_: self.root.after(0, self.resume_backups)),
        )
        items = [
            pystray.MenuItem(pause_label, None, enabled=False),
            pystray.MenuItem("Back up now", pystray.Menu(*jobs), enabled=bool(jobs)),
            pystray.MenuItem("Pause backups", pause),
        ]
        if self._notification_run:
            items.append(pystray.MenuItem("Open failed run", lambda *_: self._show_window()))
        items.extend((pystray.MenuItem("View logs", lambda *_: self.root.after(0, self._open_logs)),
                      pystray.MenuItem("Settings", lambda *_: self.root.after(0, lambda: self._show("settings"))),
                      pystray.MenuItem("Exit", self.on_exit)))
        self.tray_icon.menu = pystray.Menu(*items)
        count = len(self._notification_state.get("attention", {}))
        self.tray_icon.title = (
            f"Backer - {count} backup{'s' if count != 1 else ''} needs attention" if count else "Backer"
        )
        self.tray_icon.update_menu()

    def pause_backups(self, mode):
        now = datetime.now().astimezone()
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        until = {"hour": now + timedelta(hours=1), "tomorrow": tomorrow, "forever": None}[mode]
        self.config = self.config.model_copy(
            update={"local_scheduled_paused": True, "local_scheduled_pause_until": until}
        )
        save_schedule_pause(self.config)
        self.set_status("Paused · scheduled backups are paused")
        self._refresh_tray_menu()

    def resume_backups(self):
        self.config = self.config.model_copy(
            update={"local_scheduled_paused": False, "local_scheduled_pause_until": None}
        )
        save_schedule_pause(self.config)
        self.set_status("Scheduled backups resumed")
        self._refresh_tray_menu()

    def _open_logs(self):
        directory = get_data_dir() / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(directory)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(directory)])

    def notify(self, title, body):
        if self.tray_icon:
            self.tray_icon.notify(body, title)
        elif (sys.platform.startswith("linux") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
              and shutil.which("notify-send")):
            subprocess.run(
                ["notify-send", title, body], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        else:
            self.set_status(body)

    def _show_window(self, *_):
        def show():
            self.root.deiconify()
            self.root.lift()
            if self._notification_run:
                self.backup_job(self._notification_run)
                self._notification_run = None
                self._refresh_tray_menu()
        self.root.after(0, show)

    def on_window_close(self):
        if self.running and not self.confirm_quit_during_run():
            return
        if self.tray_icon:
            self.root.withdraw()
        else:
            if sys.platform.startswith("linux"):
                self.set_status("Scheduled backups continue from the systemd timer after this window closes")
            self.on_exit()

    def on_exit(self, *_):
        if self.running and not self.confirm_quit_during_run():
            return
        if self.tray_icon:
            self.tray_icon.stop()
        if self.running:
            self.cancel_running()
        self.alive = False
        self.root.destroy()

    def cancel_running(self):
        self.run_cancel.set()
        owner = getattr(self, "process_owner", None)
        if owner:
            threading.Thread(target=owner.cancel, daemon=True).start()

    def run(self):
        self.root.mainloop()


class RunView(ttk.Frame):
    primary = None

    def __init__(self, app):
        super().__init__(app.container, padding=18)
        self.app = app
        ttk.Label(self, text="Backup run", font=("Segoe UI", 16, "bold")).pack(anchor=tk.W)
        self.label = tk.StringVar(value="Waiting")
        ttk.Label(self, textvariable=self.label).pack(anchor=tk.W, pady=8)
        self.bar = ttk.Progressbar(self, mode="indeterminate")
        self.bar.pack(fill=tk.X)
        self.details = tk.Text(self, height=15, state=tk.DISABLED)
        self.details.pack(fill=tk.BOTH, expand=True, pady=12)
        self.primary = ttk.Button(self, text="Stop", command=self.stop, state=tk.DISABLED)
        self.primary.pack(anchor=tk.W)
        ttk.Button(self, text="Back", command=self.back).pack(anchor=tk.W, pady=6)

    def start(self, name):
        from backer.backends.kopia import KopiaProcessOwner

        self.app.running = True
        self.job_name = name
        self.app.run_cancel.clear()
        self.app.progress_frame = None
        self.app.progress_at = time.monotonic()
        self._last_progress = None
        self._last_frame = None
        self._throughput = 0
        self.app.process_owner = KopiaProcessOwner()
        self.primary.configure(state=tk.NORMAL)
        self.label.set("Scanning · first backup has no percentage")
        self.bar.configure(mode="indeterminate")
        self.bar.start(12)

        def progress(**frame):
            if not self.app.run_cancel.is_set():
                self.app.progress_frame = frame
                self.app.progress_at = time.monotonic()

        generation = self.app.generation("run")

        def worker():
            try:
                report = run_local_job(
                    self.app.config,
                    name,
                    on_progress=progress,
                    cancel_event=self.app.run_cancel,
                    process_owner=self.app.process_owner,
                )
            except KeyboardInterrupt:
                report = {"success": False, "cancelled": True, "errors": ["Backup cancelled"]}
            self.app.marshal(generation, lambda: self.done(report))

        threading.Thread(target=worker, daemon=True).start()
        self.app.root.after(200, self.tick)

    def tick(self):
        frame = self.app.progress_frame
        if self.app.running and time.monotonic() - self.app.progress_at > 5:
            self.bar.configure(mode="indeterminate")
            self.bar.start(12)
            self.label.set("Scanning · waiting for backup progress")
        elif frame:
            done = frame.get("bytes_processed", frame.get("bytes_done", 0))
            total = frame.get("total_bytes")
            now = time.monotonic()
            if frame is not self._last_frame:
                prior = self._last_progress
                self._last_progress = (done, now)
                self._last_frame = frame
                if prior and now > prior[1]:
                    self._throughput = max(0, (done - prior[0]) / (now - prior[1]))
            speed = f" · {int(self._throughput)} B/s" if self._last_progress and self._throughput else ""
            detail = ""
            if "hashed_bytes" in frame:
                detail = f" · {frame['hashed_bytes']} hashed, {frame.get('cached_bytes', 0)} cached"
            if total:
                self.bar.stop()
                self.bar.configure(mode="determinate", maximum=total, value=done)
                self.label.set(f"Running · {done} of {total} bytes{detail}{speed}")
            else:
                self.bar.configure(mode="indeterminate")
                self.label.set(f"Scanning · {done} bytes{detail}{speed}")
        if self.app.running:
            self.app.root.after(200, self.tick)

    def done(self, report):
        self.app.running = False
        self.primary.configure(state=tk.DISABLED)
        self.bar.stop()
        self.label.set("Completed" if report and report.get("success") else (
            "Cancelled" if report and report.get("cancelled") else "Failed"
        ))
        for error in (report or {}).get("errors", []):
            self.app.progress_log.append(error)
        self.details.configure(state=tk.NORMAL)
        self.details.delete("1.0", tk.END)
        self.details.insert(tk.END, "\n".join(self.app.progress_log))
        self.details.configure(state=tk.DISABLED)
        self.app.set_status(self.label.get(), error=not bool(report and report.get("success")))
        self.app.record_run_result(self.job_name, report or {})

    def stop(self):
        self.app.cancel_running()
        self.primary.configure(state=tk.DISABLED)
        self.app.set_status("Stop requested; Backer will safely disconnect after this Kopia operation", error=True)

    def back(self):
        if self.app.running:
            self.stop()
            return
        self.app._show("home")


class RestoreView(ttk.Frame):
    primary = None

    def __init__(self, app):
        super().__init__(app.container, padding=18)
        self.app, self.job_name = app, None
        ttk.Label(self, text="Restore", font=("Segoe UI", 16, "bold")).pack(anchor=tk.W)
        self.status = tk.StringVar(value="Choose a local job from Home.")
        ttk.Label(self, textvariable=self.status, style="Muted.TLabel").pack(anchor=tk.W, pady=6)
        self.snapshots = ttk.Treeview(self, columns=("time", "source"), show="headings", height=7)
        for column, label in (("time", "Snapshot"), ("source", "Source")):
            self.snapshots.heading(column, text=label)
            self.snapshots.column(column, width=300)
        self.snapshots.pack(fill=tk.X)
        folder_row = ttk.Frame(self)
        folder_row.pack(fill=tk.X, pady=8)
        self.include = tk.StringVar()
        ttk.Label(folder_row, text="Folder (empty restores all)").pack(side=tk.LEFT)
        ttk.Entry(folder_row, textvariable=self.include).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        self.mode = tk.StringVar(value="NEW")
        destination = ttk.Frame(self)
        destination.pack(fill=tk.X)
        for value, label in (("NEW", "New folder"), ("MERGE", "Another folder"), ("REPLACE", "Overwrite originals")):
            ttk.Radiobutton(destination, text=label, variable=self.mode, value=value).pack(side=tk.LEFT, padx=(0, 12))
        self.target = tk.StringVar()
        ttk.Entry(self, textvariable=self.target).pack(fill=tk.X, pady=(6, 0))
        ttk.Button(self, text="Choose destination", command=self._choose_destination).pack(anchor=tk.W, pady=4)
        self.progress = ttk.Progressbar(self, mode="indeterminate")
        self.progress.pack(fill=tk.X, pady=8)
        self.primary = ttk.Button(self, text="Restore", command=self.restore, state=tk.DISABLED)
        self.primary.pack(anchor=tk.W)
        actions = ttk.Frame(self)
        actions.pack(anchor=tk.W, pady=4)
        self.copy_error = ttk.Button(actions, text="Copy error", command=self._copy_error, state=tk.DISABLED)
        self.copy_error.pack(side=tk.LEFT)
        self.open_log = ttk.Button(actions, text="Open log folder", command=self._open_log, state=tk.DISABLED)
        self.open_log.pack(side=tk.LEFT, padx=6)
        ttk.Button(self, text="Back", command=self.back).pack(anchor=tk.W, pady=6)

    def _choose_destination(self):
        chosen = filedialog.askdirectory()
        if chosen:
            self.target.set(chosen)

    def load(self, job_name):
        self.job_name = job_name
        self.status.set("Checking repository and loading snapshots…")
        self.snapshots.delete(*self.snapshots.get_children())
        self.primary.configure(state=tk.DISABLED)
        generation = self.app.generation("restore")

        def worker():
            try:
                from backer.backends.base import BackupDestination
                from backer.cli import _repository_backend, _repository_destination

                job = self.app.config.jobs[job_name]
                repository = self.app.config.repositories[job.repository]
                backend, passphrase, storage = _repository_backend(repository)
                from backer.serverless.repositories import probe

                status, _unique_id, message = probe(repository, passphrase, storage)
                if status != "present":
                    raise ValueError(message or f"Repository is {status}")
                rows = backend.list_snapshots(BackupDestination(_repository_destination(repository)))
                result = (rows, None, message)
            except Exception as error:
                result = ([], str(error), "")
            self.app.marshal(generation, lambda: self._loaded(generation, *result))

        threading.Thread(target=worker, daemon=True).start()

    def _loaded(self, generation, rows, error, probe_message=""):
        if not self.app.current(generation):
            return
        if error:
            self.status.set(f"Repository check failed: {error}")
            self.app.set_status(self.status.get(), error=True)
            return
        for row in rows:
            snapshot = row.get("id") or row.get("snapshotID") or row.get("source", {}).get("path", "")
            self.snapshots.insert(
                "", tk.END, iid=snapshot, values=(row.get("startTime", snapshot), row.get("source", {}).get("path", ""))
            )
        self.status.set("Select a snapshot and restore destination." if rows else (
            probe_message or "No snapshots found; repository was checked."
        ))
        self.primary.configure(state=tk.NORMAL if rows else tk.DISABLED)

    def restore(self):
        if self.app.running:
            self.app.set_status("Another backup operation is already running", error=True)
            return
        selected = self.snapshots.selection()
        if not self.job_name or not selected:
            self.app.set_status("Select a snapshot first", error=True)
            return
        raw_target = self.target.get().strip()
        job = self.app.config.jobs[self.job_name]
        if not raw_target and self.mode.get() != "NEW":
            self.app.set_status("Choose a restore destination", error=True)
            return
        self.primary.configure(state=tk.DISABLED)
        self.progress.start(12)
        self.status.set("Validating restore destination…")
        mode, include, snapshot = self.mode.get(), self.include.get().strip(), selected[0]
        repository_name, original_path = job.repository, job.source.path
        generation = self.app.generation("restore")

        def worker():
            target = raw_target
            try:
                from backer.cli import _restore_prepare_destination, _restore_target

                target = str(_restore_target(Path(raw_target) if raw_target else None, mode, Path(original_path)))
                _restore_prepare_destination(Path(target), mode, config=self.app.config)
                error = None
            except Exception as exc:
                error = str(exc)
            self.app.marshal(
                generation,
                lambda: self._prepared(
                    generation, target, mode, include, snapshot, repository_name, original_path, error
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _prepared(self, generation, target, mode, include, snapshot, repository_name, original_path, error):
        if error:
            self.progress.stop()
            self.status.set(error)
            self.app.set_status(error, error=True)
            self.primary.configure(state=tk.NORMAL)
            return
        if mode == "REPLACE" and not self.app.confirm_restore_overwrite():
            self.progress.stop()
            self.status.set("Restore cancelled; destination unchanged")
            self.primary.configure(state=tk.NORMAL)
            return
        from backer.backends.kopia import KopiaProcessOwner

        self.app.running = True
        self.app.run_cancel.clear()
        self.app.process_owner = KopiaProcessOwner()
        self.status.set("Restoring…")
        self.restore_frame, self.restore_at = None, time.monotonic()
        self.primary.configure(text="Stop", command=self.stop, state=tk.NORMAL)

        def worker():
            def progress(**frame):
                self.restore_frame = frame
                self.restore_at = time.monotonic()

            try:
                from backer.cli import _repository_backend, _repository_destination
                from backer.core.runner import run_restore

                repository = self.app.config.repositories[repository_name]
                backend, passphrase, storage = _repository_backend(repository)
                options = {
                    "repository_password": passphrase,
                    "process_owner": self.app.process_owner,
                    "cancel_event": self.app.run_cancel,
                }
                if storage and repository.type == "s3":
                    options["s3"] = {
                        **storage,
                        "bucket": repository.bucket,
                        "prefix": repository.prefix or "",
                        "endpoint": repository.endpoint,
                        "region": repository.region,
                    }
                report = run_restore(
                    {
                        "job_name": self.job_name,
                        "source_path": _repository_destination(repository),
                        "destination_path": target,
                        "snapshot": snapshot,
                        "source_subfolder": include,
                        "original_source_path": original_path,
                        "clean_restore": mode == "REPLACE",
                        "repository_options": options,
                    }, on_progress=progress
                )
            except Exception as error:
                report = {"success": False, "errors": [str(error)]}
            self.app.marshal(generation, lambda: self._done(report))

        threading.Thread(target=worker, daemon=True).start()
        self.app.root.after(200, lambda: self._tick_restore(generation))

    def _tick_restore(self, generation):
        if not self.app.current(generation):
            return
        frame = getattr(self, "restore_frame", None)
        if frame:
            self._progress_frame(**frame)
        elif time.monotonic() - getattr(self, "restore_at", 0) > 5:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
            self.status.set("Restoring · scanning")
        if self.app.running:
            self.app.root.after(200, lambda: self._tick_restore(generation))

    def _progress_frame(self, *, bytes_processed=0, total_bytes=0, files_processed=0, total_files=0, **_frame):
        if total_bytes:
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=total_bytes, value=bytes_processed)
            self.status.set(f"Restoring · {files_processed} of {total_files or '?'} files")
        else:
            self.progress.configure(mode="indeterminate")
            self.status.set(f"Restoring · {bytes_processed} bytes")

    def _done(self, report):
        self.app.running = False
        self.progress.stop()
        ok = bool(report.get("success"))
        self.status.set(
            "Restore completed" if ok else (
                "Restore cancelled"
                if report.get("cancelled")
                else "; ".join(report.get("errors") or ["Restore failed"])
            )
        )
        self.app.set_status(self.status.get(), error=not ok)
        self.primary.configure(text="Restore", command=self.restore, state=tk.NORMAL)
        if not ok:
            self.copy_error.configure(state=tk.NORMAL)
            self.open_log.configure(state=tk.NORMAL)

    def _copy_error(self):
        self.app.root.clipboard_clear()
        self.app.root.clipboard_append(self.status.get())

    def _open_log(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(LOG_DIR)  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(LOG_DIR)], check=False)

    def stop(self):
        self.app.cancel_running()
        self.primary.configure(state=tk.DISABLED)
        self.status.set("Stopping restore safely…")

    def back(self):
        if self.app.running:
            self.stop()
            return
        self.app._show("home")


def _scheduled_test_command(argv=None):
    """Dispatch the only non-GUI command accepted by the frozen desktop binary."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        return None
    if len(arguments) != 2 or arguments[0] != "scheduled-test":
        raise SystemExit(2)
    from backer.serverless.scheduled_test import run

    return run(arguments[1])


def main():
    if (exit_code := _scheduled_test_command()) is not None:
        return exit_code
    logging.basicConfig(level=logging.INFO)
    BackerAgentApp().run()


if __name__ == "__main__":
    raise SystemExit(main())
