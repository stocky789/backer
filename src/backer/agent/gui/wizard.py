"""Single-window repository and job setup flow."""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import filedialog, ttk

from croniter import croniter

from backer.core import keystore
from backer.core.config import JobConfig, RepositoryConfig, ScheduleConfig, SourceConfig
from backer.core.paths import get_config_dir
from backer.core.smb_browse import SMBBrowser


def connection_conflict_message(server: str) -> str:
    """Name the existing Windows SMB connection; never hide it behind error 1219."""
    from backer.core.mounts import SMBConnectionManager

    conflict = SMBConnectionManager()._find_existing_connection(server)
    if conflict:
        share, username = conflict
        return (
            f"Windows is already connected to {share} as {username or 'another account'}. Close it or use that account."
        )
    return "Windows already has a connection to this server with different credentials."


def rollback_repository(config, config_path, repository_id: str) -> None:
    """Undo a partially-created repository, including both secret scopes."""
    record = config.repositories.pop(repository_id, None)
    if record:
        for reference in (record.passphrase_ref, record.storage_password_ref):
            if reference:
                keystore.delete(reference, machine_scope=False)
                keystore.delete(reference, machine_scope=True)
    config.save(config_path)


class RepositoryWizard(ttk.Frame):
    primary = None

    def __init__(self, app):
        super().__init__(app.container, padding=18)
        self.app = app
        self.step = 1
        self.cancel = threading.Event()
        self._generation = 0
        self.values = {
            "type": "local",
            "path": "",
            "name": "Repository",
            "passphrase": "",
            "source": "",
            "schedule": "0 2 * * *",
        }
        self.body = ttk.Frame(self)
        self.body.pack(fill=tk.BOTH, expand=True)
        self._render()

    def show(self):
        self._render()

    def _clear(self):
        self.cancel.set()
        self._generation += 1
        for child in self.body.winfo_children():
            child.destroy()

    def _render(self):
        self._clear()
        self.cancel = threading.Event()
        if self.step == 1:
            self._storage()
        elif self.step == "s3":
            self._s3()
        elif self.step == 2:
            self._server()
        elif self.step == 3:
            self._share()
        elif self.step == 4:
            self._passphrase()
        elif self.step == 5:
            self._job()
        else:
            self._review()

    def _buttons(self, next_command, label="Continue"):
        row = ttk.Frame(self.body)
        row.pack(fill=tk.X, pady=16)
        if self.step > 1:
            ttk.Button(row, text="Back", command=lambda: self._go(self.step - 1)).pack(side=tk.LEFT)
        self.primary = ttk.Button(row, text=label, command=next_command)
        self.primary.pack(side=tk.RIGHT)

    def _go(self, step):
        self.step = step
        self._render()

    def _storage(self):
        ttk.Label(self.body, text="Add repository", font=("Segoe UI", 16, "bold")).pack(anchor=tk.W)
        kind = tk.StringVar(value=self.values["type"])
        for value, label in (
            ("smb", "Network share"),
            ("local", "Drive on this computer"),
            ("s3", "S3-compatible storage"),
        ):
            ttk.Radiobutton(self.body, text=label, value=value, variable=kind).pack(anchor=tk.W, pady=4)

        def next_step():
            self.values["type"] = kind.get()
            if kind.get() == "local":
                self.values["path"] = filedialog.askdirectory() or self.values["path"]
                self._go(4)
            elif kind.get() == "s3":
                self._go("s3")
            else:
                self._go(2)

        self._buttons(next_step)

    def _s3(self):
        ttk.Label(self.body, text="S3-compatible storage", font=("Segoe UI", 16, "bold")).pack(anchor=tk.W)
        fields = ("bucket", "prefix", "endpoint", "region", "access_key_id", "secret_key")
        entries = {}
        for key in fields:
            entries[key] = tk.StringVar(value=self.values.get(key, ""))
            ttk.Label(self.body, text=key.replace("_", " ").title()).pack(anchor=tk.W)
            ttk.Entry(self.body, textvariable=entries[key], show="*" if key == "secret_key" else "").pack(fill=tk.X)

        def next_step():
            values = {key: variable.get().strip() for key, variable in entries.items()}
            if not all(values.values()):
                self.app.set_status("All S3 fields are required", error=True)
                return
            self.values.update(values)
            self._go(4)

        self._buttons(next_step)

    def _server(self):
        ttk.Label(self.body, text="Name the file server", font=("Segoe UI", 16, "bold")).pack(anchor=tk.W)
        self.host = tk.StringVar(value=self.values.get("server", ""))
        self.username = tk.StringVar(value=self.values.get("username", ""))
        self.password = tk.StringVar(value=self.values.get("storage_password", ""))
        self.primary = ttk.Entry(self.body, textvariable=self.host)
        self.primary.pack(fill=tk.X, pady=10)
        ttk.Label(self.body, text="Username").pack(anchor=tk.W)
        ttk.Entry(self.body, textvariable=self.username).pack(fill=tk.X)
        ttk.Label(self.body, text="Password").pack(anchor=tk.W)
        ttk.Entry(self.body, textvariable=self.password, show="*").pack(fill=tk.X)

        def next_step():
            if not all((self.host.get().strip(), self.username.get().strip(), self.password.get())):
                self.app.set_status("File server credentials are required", error=True)
                return
            self.values.update(
                server=self.host.get().strip(),
                username=self.username.get().strip(),
                storage_password=self.password.get(),
            )
            self._go(3)

        self._buttons(next_step)

    def _share(self):
        ttk.Label(self.body, text="Pick the share and folder", font=("Segoe UI", 16, "bold")).pack(anchor=tk.W)
        self.listing = tk.StringVar(value="Loading shares…")
        ttk.Label(self.body, textvariable=self.listing, style="Muted.TLabel").pack(anchor=tk.W, pady=10)
        self.share = tk.StringVar()
        self.primary = ttk.Combobox(self.body, textvariable=self.share, state="readonly")
        self.primary.pack(fill=tk.X)
        generation = self._generation

        def worker():
            ok, result = SMBBrowser.list_shares(self.values["server"])

            def done():
                if generation != self._generation or self.cancel.is_set():
                    return
                if ok:
                    self.primary.configure(values=[item.name for item in result])
                    self.listing.set("Choose a share")
                else:
                    self.listing.set(str(result))

            self.app.root.after(0, done)

        threading.Thread(target=worker, daemon=True).start()
        self._buttons(
            lambda: (
                self.values.__setitem__("share", self.share.get()),
                self.values.__setitem__("path", ""),
                self._go(4),
            )
        )

    def _passphrase(self):
        ttk.Label(self.body, text="Repository passphrase", font=("Segoe UI", 16, "bold")).pack(anchor=tk.W)
        from backer.cli import _generated_passphrase

        value = self.values.get("passphrase") or _generated_passphrase()
        self.values["passphrase"] = value
        ttk.Label(self.body, text=value, style="Mono.TLabel").pack(anchor=tk.W, pady=8)
        ttk.Button(
            self.body,
            text="Copy",
            command=lambda: (self.app.root.clipboard_clear(), self.app.root.clipboard_append(value)),
        ).pack(anchor=tk.W)
        position = 1
        self.confirm = tk.StringVar()
        self.saved = tk.BooleanVar()
        ttk.Label(self.body, text=f"Enter word {position} to confirm it.").pack(anchor=tk.W, pady=(14, 0))
        entry = ttk.Entry(self.body, textvariable=self.confirm)
        entry.pack(anchor=tk.W)
        ttk.Checkbutton(
            self.body, text="I have saved this somewhere other than this computer", variable=self.saved
        ).pack(anchor=tk.W, pady=6)

        def validate(*_):
            self.primary.configure(
                state=tk.NORMAL
                if self.confirm.get().strip() == value.split("-")[position - 1] and self.saved.get()
                else tk.DISABLED
            )

        self.confirm.trace_add("write", validate)
        self.saved.trace_add("write", validate)
        self._buttons(lambda: self._go(5))
        self.primary.configure(state=tk.DISABLED)

    def _job(self):
        ttk.Label(self.body, text="Source and schedule", font=("Segoe UI", 16, "bold")).pack(anchor=tk.W)
        self.name = tk.StringVar(value=self.values["name"])
        self.source = tk.StringVar(value=self.values["source"])
        self.cron = tk.StringVar(value=self.values["schedule"])
        for label, var in (("Job name", self.name), ("Source", self.source), ("Cron", self.cron)):
            ttk.Label(self.body, text=label).pack(anchor=tk.W)
            ttk.Entry(self.body, textvariable=var).pack(fill=tk.X, pady=(0, 5))
        ttk.Button(
            self.body,
            text="Choose source",
            command=lambda: self.source.set(filedialog.askdirectory() or self.source.get()),
        ).pack(anchor=tk.W)

        def next_step():
            if not self.source.get() or not croniter.is_valid(self.cron.get()):
                self.app.set_status("Choose a source and valid schedule", error=True)
                return
            self.values.update(name=self.name.get().strip(), source=self.source.get(), schedule=self.cron.get())
            self._go(6)

        self._buttons(next_step)

    def _review(self):
        ttk.Label(self.body, text="Review", font=("Segoe UI", 16, "bold")).pack(anchor=tk.W)
        for text, step in (
            (self.values["type"], 1),
            (self.values.get("server") or self.values["path"], 2),
            (self.values["name"], 5),
            (self.values["source"], 5),
            (self.values["schedule"], 5),
        ):
            item = ttk.Label(self.body, text=text, style="Body.TLabel", cursor="hand2")
            item.pack(anchor=tk.W, pady=3)
            item.bind("<Button-1>", lambda _event, selected=step: self._go(selected))
        self._buttons(self._commit, "Create backup")

    def _commit(self):
        name = self.values["name"] or "Backup"
        repository_id = name.lower().replace(" ", "-") + "-repo"
        config_path = get_config_dir() / "config.yaml"
        created = False
        try:
            repo = RepositoryConfig(
                name=name,
                type=self.values["type"],
                path=self.values["path"] or None,
                server=self.values.get("server"),
                share=self.values.get("share"),
                username=self.values.get("username"),
                bucket=self.values.get("bucket"),
                prefix=self.values.get("prefix"),
                endpoint=self.values.get("endpoint"),
                region=self.values.get("region"),
            )
            storage = None
            if self.values["type"] == "s3":
                storage = {
                    "access_key_id": self.values["access_key_id"],
                    "secret_access_key": self.values["secret_key"],
                }
            elif self.values["type"] == "smb":
                storage = self.values.get("storage_password")
            from backer.serverless.repositories import add_repository, rescope_secrets_for_system

            repository_id, backend = add_repository(
                self.app.config,
                config_path,
                name,
                repo,
                self.values["passphrase"],
                attach=self.values.get("attach", False),
                init=not self.values.get("attach", False),
                storage=storage,
            )
            created = True
            self.app.config.jobs[name] = JobConfig(
                repository=repository_id,
                source=SourceConfig(path=self.values["source"]),
                schedule=ScheduleConfig(cron=self.values["schedule"]),
            )
            rescope_secrets_for_system(self.app.config)
            from backer.agent.gui.views import save_config

            save_config(self.app.config)
            self.app.set_status(f"Saved using {backend}")
            self.app._show("home")
        except Exception as error:
            self.app.config.jobs.pop(name, None)
            if created:
                rollback_repository(self.app.config, config_path, repository_id)
            self.app.set_status(str(error), error=True)
