"""Single-window repository and job setup flow."""

from __future__ import annotations

import os
import secrets
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from croniter import croniter

from backer.core import keystore
from backer.core.config import JobConfig, RepositoryConfig, ScheduleConfig, SourceConfig
from backer.core.paths import get_config_dir
from backer.core.smb_browse import SMBBrowser


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


class RepositoryWizard(ttk.Frame):
    primary = None

    def __init__(self, app):
        super().__init__(app.container, padding=18)
        self.app = app
        self.step = 1
        self.cancel = threading.Event()
        self._generation = 0
        self._passphrase_probe_token = 0
        self.values = {
            "type": "local",
            "path": "",
            "name": "Repository",
            "passphrase": "",
            "source": "",
            "schedule": "0 2 * * *",
            "attach": False,
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

    def _buttons(self, next_command, label="Continue", *, back=True):
        row = ttk.Frame(self.body)
        row.pack(fill=tk.X, pady=16)
        if self.step > 1:
            ttk.Button(
                row, text="Back", command=lambda: self._go(self.step - 1), state=tk.NORMAL if back else tk.DISABLED
            ).pack(side=tk.LEFT)
        self.primary = ttk.Button(row, text=label, command=next_command)
        self.primary.pack(side=tk.RIGHT)

    def _go(self, step):
        self.step = step
        self._render()

    def _record(self):
        return RepositoryConfig(
            name=self.values["name"],
            type=self.values["type"],
            path=self.values["path"] or None,
            server=self.values.get("server"),
            share=self.values.get("share"),
            username=self.values.get("username"),
            domain=self.values.get("domain"),
            use_existing_session=self.values.get("use_existing_session", False),
            bucket=self.values.get("bucket"),
            prefix=self.values.get("prefix"),
            endpoint=self.values.get("endpoint"),
            region=self.values.get("region"),
        )

    def _probe_selected_location(self):
        """Probe before asking for a passphrase so setup never guesses create versus attach."""
        record = self._record()
        storage = (
            {"access_key_id": self.values["access_key_id"], "secret_access_key": self.values["secret_key"]}
            if record.type == "s3"
            else self.values.get("storage_password")
        )
        generation = self._generation
        passphrase = self.values.get("passphrase") or "backer-location-probe"

        def worker():
            from backer.serverless.repositories import probe

            try:
                status, _unique_id, message = probe(record, passphrase, storage)
            except Exception as error:
                status, message = "unavailable", str(error)

            def done():
                if generation != self._generation or self.cancel.is_set():
                    return
                if status == "absent":
                    self.values["attach"] = False
                    self._go(4)
                elif status in {"present", "wrong_passphrase"}:
                    self.values["attach"] = True
                    self._go(4)
                else:
                    self.app.set_status(message or f"Repository is {status}", error=True)

            try:
                self.app.root.after(0, done)
            except RuntimeError:
                return

        self.app.set_status("Checking repository location")
        threading.Thread(target=worker, daemon=True).start()

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
                if self.values["path"]:
                    self._probe_selected_location()
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
            self._probe_selected_location()

        self._buttons(next_step)

    def _server(self):
        ttk.Label(self.body, text="Name the file server", font=("Segoe UI", 16, "bold")).pack(anchor=tk.W)
        self.host = tk.StringVar(value=self.values.get("server", ""))
        self.username = tk.StringVar(value=self.values.get("username", ""))
        self.password = tk.StringVar(value=self.values.get("storage_password", ""))
        self.domain = tk.StringVar(value=self.values.get("domain", ""))
        self.primary = ttk.Entry(self.body, textvariable=self.host)
        self.primary.pack(fill=tk.X, pady=10)
        ttk.Label(self.body, text="Username").pack(anchor=tk.W)
        ttk.Entry(self.body, textvariable=self.username).pack(fill=tk.X)
        ttk.Label(self.body, text="Password").pack(anchor=tk.W)
        ttk.Entry(self.body, textvariable=self.password, show="*").pack(fill=tk.X)
        ttk.Label(self.body, text="Domain (optional)").pack(anchor=tk.W)
        ttk.Entry(self.body, textvariable=self.domain).pack(fill=tk.X)
        self.attach = tk.BooleanVar(value=self.values.get("attach", False))
        ttk.Checkbutton(self.body, text="Connect to an existing repository", variable=self.attach).pack(
            anchor=tk.W, pady=6
        )

        def next_step():
            if not all((self.host.get().strip(), self.username.get().strip(), self.password.get())):
                self.app.set_status("File server credentials are required", error=True)
                return
            self.values.update(
                server=self.host.get().strip(),
                username=self.username.get().strip(),
                storage_password=self.password.get(),
                domain=self.domain.get().strip() or None,
                attach=self.attach.get(),
            )
            self._go(3)

        self._buttons(next_step)

    def _share(self):
        ttk.Label(self.body, text="Pick the share and folder", font=("Segoe UI", 16, "bold")).pack(anchor=tk.W)
        self.listing = tk.StringVar(value="Loading shares…")
        ttk.Label(self.body, textvariable=self.listing, style="Muted.TLabel").pack(anchor=tk.W, pady=10)
        self.share = tk.StringVar(value=self.values.get("share", ""))
        self.primary = ttk.Combobox(self.body, textvariable=self.share, state="readonly")
        self.primary.pack(fill=tk.X)
        self.tree = ttk.Treeview(self, columns=("path",), show="tree", height=8)
        self.tree.pack(fill=tk.BOTH, expand=True, pady=8)
        self.resolved = tk.StringVar(value="")
        ttk.Label(self.body, textvariable=self.resolved, style="Muted.TLabel").pack(anchor=tk.W)
        self.share.trace_add("write", lambda *_: self._load_folder(""))
        self.tree.bind("<<TreeviewOpen>>", self._expand_folder)
        self.tree.bind("<<TreeviewSelect>>", self._select_folder)
        ttk.Button(self.body, text="New folder", command=self._new_folder).pack(anchor=tk.W, pady=(4, 0))
        generation = self._generation

        def worker():
            ok, result = SMBBrowser.list_shares(
                self.values["server"],
                self.values.get("username"),
                self.values.get("storage_password"),
                self.values.get("domain"),
            )

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

        def next_step():
            if not self.share.get() or not self.tree.selection():
                self.app.set_status("Choose a share and folder", error=True)
                return
            self.values["share"] = self.share.get()
            self.values["path"] = self.tree.set(self.tree.selection()[0], "path")
            self._probe_selected_location()

        self._buttons(next_step)

    def _load_folder(self, path):
        share = self.share.get()
        if not share:
            return
        generation = self._generation
        server = self.values["server"]
        username = self.values.get("username")
        password = self.values.get("storage_password")
        domain = self.values.get("domain")

        def worker():
            ok, result = SMBBrowser.list_directory(server, share, path, username, password, domain)

            def done():
                if generation != self._generation or self.cancel.is_set() or not ok:
                    if not ok:
                        self._show_1219(server, str(result))
                    return
                parent = (
                    ""
                    if not path
                    else next((item for item in self.tree.get_children("") if self.tree.set(item, "path") == path), "")
                )
                if not path:
                    self.tree.delete(*self.tree.get_children())
                for entry in result:
                    if entry.is_dir:
                        child_path = "/".join(item for item in (path, entry.name) if item)
                        item = self.tree.insert(parent, tk.END, text=entry.name, values=(child_path,))
                        self.tree.insert(item, tk.END, text="Loading…")

            self.app.root.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def _expand_folder(self, _event=None):
        item = self.tree.focus()
        if item:
            self.tree.delete(*self.tree.get_children(item))
            self._load_folder(self.tree.set(item, "path"))

    def _select_folder(self, _event=None):
        item = self.tree.focus()
        if item:
            self.resolved.set(
                "Repository path: \\\\"
                + "\\".join((self.values["server"], self.share.get(), self.tree.set(item, "path")))
            )

    def _new_folder(self):
        # Keep this inline and non-modal: a temporary entry is safer than an unvalidated prompt.
        name = tk.StringVar()
        entry = ttk.Entry(self.body, textvariable=name)
        entry.pack(fill=tk.X, pady=4)
        entry.focus_set()

        def create_folder(_event=None):
            parent = self.tree.focus()
            path = self.tree.set(parent, "path") if parent else ""
            full_path = "/".join(item for item in (path, name.get().strip()) if item)
            server, share = self.values["server"], self.share.get()
            username = self.values.get("username")
            password = self.values.get("storage_password")
            domain = self.values.get("domain")
            generation = self._generation
            entry.configure(state=tk.DISABLED)
            def worker():
                ok = SMBBrowser.make_directory(server, share, full_path, username, password, domain)
                def done():
                    if generation != self._generation or self.cancel.is_set():
                        return
                    if ok:
                        entry.destroy()
                        self._load_folder(path)
                    else:
                        entry.configure(state=tk.NORMAL)
                        self.app.set_status("Could not create folder", error=True)
                self.app.root.after(0, done)
            threading.Thread(target=worker, daemon=True).start()

        entry.bind("<Return>", create_folder)

    def _show_1219(self, server, error):
        if "1219" not in error:
            self.listing.set(error)
            return
        from backer.core.mounts import SMBConnectionManager

        manager = SMBConnectionManager()
        conflict = manager._find_existing_connection(server)
        connection = conflict[0] if conflict else f"\\\\{server}"
        self.listing.set(connection_conflict_message(server, conflict))
        actions = ttk.Frame(self.body)
        actions.pack(anchor=tk.W, pady=4)

        def use_existing():
            selected_share = self.share.get().strip() or self.values.get("share", "")
            selected_path = self.values.get("path", "")
            selected = self.tree.selection()
            if selected:
                selected_path = self.tree.set(selected[0], "path")
            if not selected_share:
                self.listing.set("Choose the backup share and folder before reusing an existing Windows connection.")
                return
            generation = self._generation

            def worker():
                ok = manager.connect_existing_serverless(server, selected_share, selected_path)

                def done():
                    if generation != self._generation or self.cancel.is_set():
                        return
                    if ok:
                        self.values.update(
                            share=selected_share,
                            path=selected_path,
                            storage_password=None,
                            use_existing_session=True,
                        )
                        self.app.set_status(
                            f"Reused the sign-in on {connection} and tested \\\\{server}\\{selected_share}"
                        )
                        self._probe_selected_location()
                    else:
                        self.listing.set(
                            f"Backer could not write a temporary probe in \\\\{server}\\{selected_share}. "
                            "Nothing was disconnected."
                        )

                try:
                    self.app.root.after(0, done)
                except RuntimeError:
                    return

            threading.Thread(target=worker, daemon=True).start()

        def disconnect_existing():
            if not self.app.confirm_remove_repository(connection):
                return
            generation = self._generation

            def worker():
                ok = manager.disconnect_existing_connection(connection)

                def done():
                    if generation != self._generation or self.cancel.is_set():
                        return
                    if ok:
                        self.app.set_status(f"Disconnected {connection}; enter the credentials to continue")
                        self._go(2)
                    else:
                        self.listing.set(f"Could not disconnect {connection}. Nothing else was changed.")

                try:
                    self.app.root.after(0, done)
                except RuntimeError:
                    return

            threading.Thread(target=worker, daemon=True).start()

        ttk.Button(actions, text="Use existing connection", command=use_existing).pack(side=tk.LEFT)
        ttk.Button(actions, text=f"Disconnect {connection}", command=disconnect_existing).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="Cancel", command=lambda: self._go(2)).pack(side=tk.LEFT, padx=6)

    def _passphrase(self):
        ttk.Label(self.body, text="Repository passphrase", font=("Segoe UI", 16, "bold")).pack(anchor=tk.W)
        from backer.cli import _generated_passphrase

        if self.values.get("attach"):
            candidate = tk.StringVar()
            status = tk.StringVar(value="Enter the existing repository passphrase to verify this location.")
            ttk.Label(self.body, textvariable=status, style="Muted.TLabel").pack(anchor=tk.W, pady=8)
            entry = ttk.Entry(self.body, textvariable=candidate, show="*")
            entry.pack(fill=tk.X)
            self.primary = entry

            def validate_existing(*_):
                self.values["passphrase"] = candidate.get()
                self._passphrase_probe_token += 1
                token = self._passphrase_probe_token
                self.primary.configure(state=tk.DISABLED)
                if not candidate.get():
                    status.set("Enter the existing repository passphrase to verify this location.")
                    return
                generation = self._generation
                record = self._record()
                storage = (
                    {"access_key_id": self.values["access_key_id"], "secret_access_key": self.values["secret_key"]}
                    if record.type == "s3"
                    else self.values.get("storage_password")
                )
                passphrase = candidate.get()

                def worker():
                    from backer.serverless.repositories import probe

                    try:
                        result, _unique_id, message = probe(record, passphrase, storage)
                    except Exception as error:
                        result, message = "unavailable", str(error)

                    def done():
                        if (
                            generation != self._generation
                            or self.cancel.is_set()
                            or token != self._passphrase_probe_token
                            or candidate.get() != passphrase
                        ):
                            return
                        if result == "present":
                            status.set("Repository verified")
                            self.primary.configure(state=tk.NORMAL)
                        else:
                            status.set(message or "Passphrase was not accepted")

                    try:
                        self.app.root.after(0, done)
                    except RuntimeError:
                        return

                threading.Thread(target=worker, daemon=True).start()

            candidate.trace_add("write", validate_existing)
            self._buttons(lambda: self._go(5))
            self.primary.configure(state=tk.DISABLED)
            return
        value = self.values.get("passphrase") or _generated_passphrase()
        self.values["passphrase"] = value
        self.passphrase_frames = (ttk.Frame(self.body), ttk.Frame(self.body))
        reveal, confirm = self.passphrase_frames
        ttk.Label(reveal, text="Recovery copy", font=("Segoe UI", 12, "bold")).pack(anchor=tk.W)
        ttk.Label(
            reveal,
            text=(
                "This passphrase is the only recovery key for this repository. "
                "If it is lost, the backups cannot be restored."
            ),
            style="Danger.TLabel",
            wraplength=640,
        ).pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(reveal, text=value, style="Mono.TLabel").pack(anchor=tk.W, pady=8)
        ttk.Label(
            reveal,
            text=(
                "Save or print a recovery record somewhere off this computer. "
                f"Backer stores it using {keystore.backend_name()} on this computer."
            ),
            style="Muted.TLabel",
            wraplength=640,
        ).pack(anchor=tk.W)
        ttk.Button(reveal, text="Copy (clipboard is not storage)", command=lambda: self._copy_passphrase(value)).pack(
            anchor=tk.W, pady=8
        )
        ttk.Button(reveal, text="Save recovery record", command=lambda: self._save_recovery_record(value)).pack(
            anchor=tk.W
        )
        ttk.Button(
            reveal, text="Save and print recovery record", command=lambda: self._print_recovery_record(value)
        ).pack(anchor=tk.W, pady=(4, 8))
        ttk.Button(reveal, text="I saved the recovery copy", command=lambda: self._show_passphrase_frame(confirm)).pack(
            anchor=tk.W
        )
        position = secrets.randbelow(len(value.split("-"))) + 1
        self.confirm = tk.StringVar()
        self.saved = tk.BooleanVar()
        ttk.Label(confirm, text=f"Enter word {position} to confirm it.").pack(anchor=tk.W, pady=(14, 0))
        ttk.Label(confirm, text="The recovery copy is no longer shown on this screen.", style="Muted.TLabel").pack(
            anchor=tk.W
        )
        entry = ttk.Entry(confirm, textvariable=self.confirm)
        entry.pack(anchor=tk.W)
        ttk.Checkbutton(
            confirm, text="I have saved this somewhere other than this computer", variable=self.saved
        ).pack(anchor=tk.W, pady=6)

        def validate(*_):
            self.primary.configure(
                state=tk.NORMAL
                if self.confirm.get().strip() == value.split("-")[position - 1] and self.saved.get()
                else tk.DISABLED
            )

        self.confirm.trace_add("write", validate)
        self.saved.trace_add("write", validate)
        self._buttons(lambda: self._go(5), back=False)
        self.primary.configure(state=tk.DISABLED)
        self._show_passphrase_frame(reveal)

    def _show_passphrase_frame(self, frame):
        for candidate in self.passphrase_frames:
            candidate.pack_forget()
        frame.pack(fill=tk.BOTH, expand=True)

    def _copy_passphrase(self, value):
        self.app.root.clipboard_clear()
        self.app.root.clipboard_append(value)

    def _save_recovery_record(self, value):
        target = filedialog.asksaveasfilename(title="Save recovery record", defaultextension=".txt")
        if not target:
            return None
        path = Path(target)
        path.write_text(value + "\n", encoding="utf-8")
        if os.name != "nt":
            path.chmod(0o600)
        self.app.set_status("Recovery record saved; keep it off this computer")
        return path

    def _print_recovery_record(self, value):
        path = self._save_recovery_record(value)
        if not path:
            return
        if os.name == "nt":
            os.startfile(str(path), "print")
        else:
            subprocess.run(["lpr", str(path)], check=False)

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
                domain=self.values.get("domain"),
                use_existing_session=self.values.get("use_existing_session", False),
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

            # add_repository has its own transactional cleanup for a failure
            # after writing secrets/config.  Mark the id pending before call;
            # if a future helper reports it on failure this remains safe.
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
            rollback_errors = rollback_repository(self.app.config, config_path, repository_id) if created else []
            message = str(error)
            if rollback_errors:
                message += "; " + "; ".join(rollback_errors)
            self.app.set_status(message, error=True)
