"""Retained Tk views over the unified local configuration."""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk

from backer.core.config import BackerConfig, ClientConfig
from backer.core.config import load_config as _load_config
from backer.core.paths import get_config_dir, get_data_dir
from backer.serverless.store import read_runs


def load_config() -> BackerConfig:
    """Phase 1 config wrapper retained for the desktop application."""
    return _load_config()


def save_config(config: BackerConfig) -> None:
    """Phase 1 config wrapper retained for the desktop application."""
    config.save(get_config_dir() / "config.yaml")


class HomeView(ttk.Frame):
    primary = None

    def __init__(self, app):
        super().__init__(app.container, padding=18)
        self.app = app
        self.tree = ttk.Treeview(self, columns=("source", "repository", "schedule", "last"), show="headings")
        for column, label, width in (
            ("source", "Source", 220),
            ("repository", "Repository", 150),
            ("schedule", "Schedule", 120),
            ("last", "Last run", 140),
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
        self.restore = ttk.Button(buttons, text="Restore", command=lambda: app._show("restore"), state=tk.DISABLED)
        self.restore.pack(side=tk.LEFT)
        self.remove = ttk.Button(buttons, text="Remove", command=app.remove_selected_job, state=tk.DISABLED)
        self.remove.pack(side=tk.RIGHT)
        self.tree.bind("<<TreeviewSelect>>", self._selection)
        self.empty = ttk.Label(self, text="No local backup jobs yet. Add a repository to begin.", style="Muted.TLabel")
        self.primary = self.add

    def _selection(self, _event=None):
        state = tk.NORMAL if self.tree.selection() else tk.DISABLED
        for button in (self.backup, self.restore, self.remove):
            button.configure(state=state)

    def refresh(self):
        self.empty.pack_forget()
        self.tree.delete(*self.tree.get_children())
        for name, job in self.app.config.jobs.items():
            repository = self.app.config.repositories.get(job.repository)
            schedule = job.schedule.cron if job.schedule and job.schedule.cron else "Manual"
            self.tree.insert(
                "",
                tk.END,
                iid=name,
                values=(job.source.path, repository.name if repository else "Missing", schedule, "…"),
            )
        if not self.app.config.jobs:
            self.empty.place(relx=0.5, rely=0.45, anchor=tk.CENTER)
        self._selection()
        self._refresh_runs()

    def _refresh_runs(self):
        generation = self.app.generation("home")

        def worker():
            rows = {}
            for name in self.app.config.jobs:
                runs = read_runs(get_data_dir(), name, 1)
                rows[name] = "Never run" if not runs else runs[0].status.value.replace("_", " ").title()
            self.app.root.after(0, lambda: self._apply_runs(generation, rows))

        threading.Thread(target=worker, daemon=True).start()
        self.app.root.after(60000, lambda: self._refresh_runs() if self.app.visible == "home" else None)

    def _apply_runs(self, generation, rows):
        if not self.app.current(generation):
            return
        for name, status in rows.items():
            if self.tree.exists(name):
                values = list(self.tree.item(name, "values"))
                values[-1] = status
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
        ttk.Label(self, text="Settings", font=("Segoe UI", 16, "bold")).pack(anchor=tk.W)
        self.server = tk.StringVar(value=app.config.server.server_url if app.config.server else "")
        ttk.Label(self, text="Server address").pack(anchor=tk.W, pady=(14, 0))
        self.primary = ttk.Entry(self, textvariable=self.server, width=58)
        self.primary.pack(fill=tk.X)
        self.mode = tk.StringVar(value="system")
        ttk.Label(self, text="Appearance").pack(anchor=tk.W, pady=(14, 0))
        ttk.Combobox(self, textvariable=self.mode, values=("system", "light", "dark"), state="readonly").pack(
            anchor=tk.W
        )
        self.unattended = ttk.Button(self, text="Set up unattended scheduled backups", command=self._unattended)
        self.unattended.pack(anchor=tk.W, pady=(14, 0))
        ttk.Button(self, text="Save settings", command=self._save).pack(anchor=tk.W, pady=6)
        ttk.Button(self, text="Back", command=lambda: app._show("home")).pack(anchor=tk.W)

    def _save(self):
        url = self.server.get().strip()
        self.app.config.server = ClientConfig(server_url=url) if url else None
        save_config(self.app.config)
        self.app.set_status("Settings saved")
        self.app.apply_theme(self.mode.get())

    def _unattended(self):
        from backer.client.windows_service import create_background_scheduled_task

        ok, message = create_background_scheduled_task(self.server.get().strip() or None)
        self.app.set_status(message, error=not ok)
