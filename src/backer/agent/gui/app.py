"""Backer's single-window desktop companion for local and server-managed backups."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from backer.agent.gui import theme
from backer.agent.gui.views import HomeView, SettingsView, SimpleView, load_config, save_config
from backer.agent.gui.wizard import RepositoryWizard
from backer.core.messages import explain_failure
from backer.core.paths import get_data_dir
from backer.serverless.runs import run_local_job

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
        self.container = ttk.Frame(self.root)
        self.container.pack(fill=tk.BOTH, expand=True)
        self.status_var = tk.StringVar(value="OK · Ready")
        self.subtitle_var = tk.StringVar(value="Local backups")
        self._setup_menu()
        self._setup_status()
        self.apply_theme()
        self._make_tray()
        self.root.protocol("WM_DELETE_WINDOW", self.on_window_close)
        self._show("welcome" if not self.config.repositories and not self.config.server else "home")

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
        ttk.Label(strip, textvariable=self.subtitle_var, style="Muted.TLabel").pack(side=tk.RIGHT)

    def apply_theme(self, override=None):
        self.tokens = theme.apply(ttk.Style(self.root), override)

    def generation(self, name):
        self._generations[name] = self._generations.get(name, 0) + 1
        return name, self._generations[name]

    def current(self, token):
        return self._generations.get(token[0]) == token[1]

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

    def backup_selected(self):
        home = self.views.get("home")
        if home and home.tree.selection():
            self._show("run")
            self.views["run"].start(home.tree.selection()[0])

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

    def confirm_remove_repository(self):
        return messagebox.askyesno("Remove repository", "Remove this repository configuration?", parent=self.root)

    def confirm_quit_during_run(self):
        return messagebox.askyesno(
            "Quit during backup", "The running backup will be cancelled. Quit?", parent=self.root
        )

    def confirm_reveal_passphrase(self):
        return messagebox.askyesno("Reveal passphrase", "Reveal the repository passphrase?", parent=self.root)

    def _make_tray(self):
        if not TRAY_AVAILABLE:
            return
        try:
            image = Image.open(str(APP_DIR / "backer.ico"))
            self.tray_icon = pystray.Icon(
                "backer",
                image,
                "Backer",
                pystray.Menu(pystray.MenuItem("Open", self._show_window), pystray.MenuItem("Quit", self.on_exit)),
            )
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        except Exception:
            self.tray_icon = None

    def notify(self, title, body):
        if self.tray_icon:
            self.tray_icon.notify(body, title)
        elif sys.platform != "win32" and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            subprocess.run(["notify-send", title, body], check=False)
        else:
            self.set_status(body)

    def _show_window(self, *_):
        self.root.after(0, lambda: (self.root.deiconify(), self.root.lift()))

    def on_window_close(self):
        if self.running and not self.confirm_quit_during_run():
            return
        if self.tray_icon:
            self.root.withdraw()
        else:
            self.on_exit()

    def on_exit(self, *_):
        if self.running and not self.confirm_quit_during_run():
            return
        if self.tray_icon:
            self.tray_icon.stop()
        self.alive = False
        self.root.destroy()

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
        ttk.Button(self, text="Back", command=lambda: app._show("home")).pack(anchor=tk.W, pady=6)

    def start(self, name):
        self.app.running = True
        self.app.run_cancel.clear()
        self.app.progress_frame = None
        self.app.progress_at = time.monotonic()
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
            report = run_local_job(self.app.config, name, on_progress=progress)
            self.app.root.after(0, lambda: self.done(report) if self.app.current(generation) else None)

        threading.Thread(target=worker, daemon=True).start()
        self.app.root.after(200, self.tick)

    def tick(self):
        frame = self.app.progress_frame
        if self.app.running and time.monotonic() - self.app.progress_at > 5:
            self.bar.configure(mode="indeterminate")
            self.bar.start(12)
            self.label.set("Scanning · waiting for backup progress")
        elif frame:
            done = frame.get("bytes_done", 0)
            total = frame.get("total_bytes")
            if total:
                self.bar.stop()
                self.bar.configure(mode="determinate", maximum=total, value=done)
                self.label.set(f"Running · {done} of {total} bytes")
            else:
                self.bar.configure(mode="indeterminate")
                self.label.set(f"Scanning · {done} bytes")
        if self.app.running:
            self.app.root.after(200, self.tick)

    def done(self, report):
        self.app.running = False
        self.primary.configure(state=tk.DISABLED)
        self.bar.stop()
        self.label.set("Completed" if report and report.get("success") else "Failed")
        for error in (report or {}).get("errors", []):
            self.app.progress_log.append(error)
        self.details.configure(state=tk.NORMAL)
        self.details.delete("1.0", tk.END)
        self.details.insert(tk.END, "\n".join(self.app.progress_log))
        self.details.configure(state=tk.DISABLED)
        self.app.set_status(self.label.get(), error=not bool(report and report.get("success")))

    def stop(self):
        self.app.run_cancel.set()
        self.app.generation("run")
        self.primary.configure(state=tk.DISABLED)
        self.app.set_status("Stop requested; Backer will safely disconnect after this Kopia operation", error=True)


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
        ttk.Button(self, text="Back", command=lambda: app._show("home")).pack(anchor=tk.W, pady=6)

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
                result = (rows, None)
            except Exception as error:
                result = ([], str(error))
            self.app.root.after(0, lambda: self._loaded(generation, *result))

        threading.Thread(target=worker, daemon=True).start()

    def _loaded(self, generation, rows, error):
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
        self.status.set(
            "Select a snapshot and restore destination." if rows else "No snapshots found; repository was checked."
        )
        self.primary.configure(state=tk.NORMAL if rows else tk.DISABLED)

    def restore(self):
        selected = self.snapshots.selection()
        if not self.job_name or not selected:
            self.app.set_status("Select a snapshot first", error=True)
            return
        target = self.target.get().strip()
        job = self.app.config.jobs[self.job_name]
        if self.mode.get() == "NEW":
            from backer.cli import _restore_target

            target = str(_restore_target(Path(target) if target else None, "NEW", Path(job.source.path)))
        if not target:
            self.app.set_status("Choose a restore destination", error=True)
            return
        from backer.cli import _restore_prepare_destination

        try:
            _restore_prepare_destination(Path(target), self.mode.get(), config=self.app.config)
        except Exception as error:
            self.app.set_status(str(error), error=True)
            return
        if self.mode.get() == "REPLACE" and not self.app.confirm_restore_overwrite():
            return
        self.primary.configure(state=tk.DISABLED)
        self.progress.start(12)
        self.status.set("Restoring…")

        def worker():
            try:
                from backer.cli import _repository_backend, _repository_destination
                from backer.core.runner import run_restore

                repository = self.app.config.repositories[job.repository]
                backend, passphrase, storage = _repository_backend(repository)
                options = {"repository_password": passphrase}
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
                        "snapshot": selected[0],
                        "source_subfolder": self.include.get().strip(),
                        "original_source_path": job.source.path,
                        "clean_restore": self.mode.get() == "REPLACE",
                        "repository_options": options,
                    }
                )
            except Exception as error:
                report = {"success": False, "errors": [str(error)]}
            self.app.root.after(0, lambda: self._done(report))

        threading.Thread(target=worker, daemon=True).start()

    def _done(self, report):
        self.progress.stop()
        ok = bool(report.get("success"))
        self.status.set("Restore completed" if ok else "; ".join(report.get("errors") or ["Restore failed"]))
        self.app.set_status(self.status.get(), error=not ok)
        self.primary.configure(state=tk.NORMAL)


def main():
    logging.basicConfig(level=logging.INFO)
    BackerAgentApp().run()


if __name__ == "__main__":
    main()
