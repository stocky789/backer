"""Backer agent - runs on client machines and executes backups."""

import logging
import os
import platform
import shutil  # noqa: F401
import signal
import socket
import subprocess  # noqa: F401
import sys
import threading
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from backer import __version__
from backer.core import runner
from backer.core.config import ClientConfig, load_config
from backer.core.destination import prepare_destination, prepare_source
from backer.core.mounts import (
    check_cifs_available,
    check_nfs_available,
    is_nfs_path,
    is_smb_path,
    nfs_mount_context,
    parse_nfs_path,
    parse_smb_path,
    smb_mount_context,
)
from backer.core.paths import get_config_dir, get_data_dir  # noqa: F401
from backer.core.runner import (
    run_backup,
    run_restore,
)

_backend_for_location = runner._backend_for_location
_log_repository_options = runner._log_repository_options
_redact_repository_options = runner._redact_repository_options
_runner_write_metadata_to_path = runner._write_metadata_to_path
_runner_write_repo_metadata = runner._write_repo_metadata
_runner_write_restore_metadata = runner._write_restore_metadata

logger = logging.getLogger(__name__)

class BackerAgent:
    """Agent that runs on client machines.

    Responsibilities:
    - Register with the server
    - Send periodic heartbeats
    - Execute backup commands from server
    - Report results back to server
    """

    def __init__(
        self,
        server_url: str,
        client_id: str | None = None,
        client_secret: str | None = None,
        config_path: Path | None = None,
    ):
        self.server_url = server_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.config_path = config_path or get_config_dir() / "config.yaml"
        self.hostname = socket.gethostname()

        # Threading synchronization
        self._stop_event = threading.Event()  # Replaces _running boolean
        self._http_client_lock = threading.Lock()  # Protects HTTP client
        self._active_jobs_lock = threading.Lock()  # Protects active jobs set
        self._active_jobs: set[str] = set()  # Track running job names

        self._heartbeat_thread: threading.Thread | None = None
        self._http_client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        """Get or create HTTP client (thread-safe)."""
        with self._http_client_lock:
            if self._http_client is None:
                auth = None
                if self.client_id and self.client_secret:
                    auth = (self.client_id, self.client_secret)
                self._http_client = httpx.Client(
                    base_url=self.server_url,
                    auth=auth,
                    timeout=35.0,  # Server uses 25s long-polling, need longer timeout
                )
            return self._http_client

    def register(self, enrollment_token: str | None = None) -> tuple[str, str]:
        """Register this agent with the server.

        Returns:
            Tuple of (client_id, client_secret)
        """
        client = self._get_client()

        response = client.post(
            "/api/v1/clients/register",
            json={
                "hostname": self.hostname,
                "version": __version__,
                "os_info": f"{platform.system()} {platform.release()}",
                "tags": [],
                "enrollment_token": enrollment_token,
            },
        )
        response.raise_for_status()
        data = response.json()

        self.client_id = data["client_id"]
        self.client_secret = data["client_secret"]

        # Save credentials
        self._save_credentials()

        # Recreate client with auth (thread-safe)
        with self._http_client_lock:
            self._http_client = None

        return self.client_id, self.client_secret

    def _save_credentials(self) -> None:
        """Save client credentials to config file."""
        from backer.core import keystore

        config = load_config(self.config_path)
        config.agent_id = self.client_id or config.agent_id
        secret_ref = f"backer/server/{config.agent_id}/secret"
        if self.client_secret:
            keystore.put(secret_ref, self.client_secret)
        config.server = ClientConfig(
            server_url=self.server_url, client_id=self.client_id or "", client_secret_ref=secret_ref
        )
        config.save(self.config_path)

    @classmethod
    def from_config(cls, config_path: Path | None = None) -> "BackerAgent":
        """Load agent from saved configuration."""
        config_path = config_path or get_config_dir() / "config.yaml"
        if config_path.exists():
            config = load_config(config_path)
            if config.server is None:
                raise FileNotFoundError(f"Agent config not found: {config_path}")
            secret = config.server.client_secret
            if config.server.client_secret_ref:
                from backer.core import keystore

                secret = keystore.get(config.server.client_secret_ref) or secret
            elif secret:
                from backer.core import keystore

                secret_ref = f"backer/server/{config.server.client_id}/secret"
                try:
                    keystore.put(secret_ref, secret)
                    if keystore.get(secret_ref) == secret:
                        config.server = config.server.model_copy(
                            update={"client_secret": "", "client_secret_ref": secret_ref}
                        )
                        config.save(config_path)
                except (OSError, RuntimeError):
                    pass
            return cls(config.server.server_url, config.server.client_id, secret, config_path)
        legacy_path = get_config_dir() / "agent.yaml"
        if not legacy_path.exists():
            raise FileNotFoundError(f"Agent config not found: {config_path}")
        import yaml

        legacy = yaml.safe_load(legacy_path.read_text(encoding="utf-8")) or {}
        return cls(legacy["server_url"], legacy.get("client_id"), legacy.get("client_secret"), legacy_path)

    def heartbeat(self) -> dict[str, Any]:
        """Send heartbeat to server and get any pending commands."""
        client = self._get_client()

        response = client.post(
            "/api/v1/clients/heartbeat",
            json={
                "client_id": self.client_id,
                "status": "online",
            },
        )
        response.raise_for_status()
        return response.json()

    def _heartbeat_loop(self, interval: int = 60) -> None:
        """Background heartbeat loop.

        Uses long-polling: the server holds the heartbeat request until
        a command is available (up to 25s), so we get commands instantly.
        We only sleep briefly between heartbeats to allow quick shutdown.

        The interval parameter is only used on connection errors to avoid
        hammering the server.
        """
        while not self._stop_event.is_set():
            try:
                result = self.heartbeat()
                # Process any pending commands
                for cmd in result.get("commands", []):
                    self._handle_command(cmd)
            except Exception as e:
                print(f"Heartbeat failed: {e}")
                # On error, wait before retry to avoid hammering server
                if self._stop_event.wait(timeout=5):
                    break
                continue

            # Brief pause between heartbeats (server handles the waiting)
            if self._stop_event.wait(timeout=1):
                break

    def _handle_command(self, command: dict[str, Any]) -> None:
        """Handle a command from the server."""
        cmd_type = command.get("command_type")
        cmd_id = command.get("id")
        payload = command.get("payload", {})

        print(f"Received command: {cmd_type} (id={cmd_id})")

        try:
            if cmd_type == "backup":
                # Run backup in background thread so heartbeat continues
                job_data = {**command, **payload}
                dry_run = payload.get("dry_run", False)
                job_name = job_data.get("job_name", "unknown")

                # Check if this job is already running
                with self._active_jobs_lock:
                    if job_name in self._active_jobs:
                        print(f"[WARN] Job '{job_name}' is already running, skipping duplicate")
                        return
                    self._active_jobs.add(job_name)

                backup_thread = threading.Thread(
                    target=self._run_backup_worker,
                    args=(job_data, dry_run, job_name, cmd_id),
                    daemon=True,
                    name=f"backup-{job_name}",
                )
                backup_thread.start()

            elif cmd_type == "restore":
                # Run restore in background thread so heartbeat continues
                dry_run = payload.get("dry_run", False)
                job_name = payload.get("job_name", "unknown")

                # Check if this job is already running
                with self._active_jobs_lock:
                    restore_key = f"restore-{job_name}"
                    if restore_key in self._active_jobs:
                        print(f"[WARN] Restore '{job_name}' is already running, skipping duplicate")
                        return
                    self._active_jobs.add(restore_key)

                restore_thread = threading.Thread(
                    target=self._run_restore_worker,
                    args=(payload, dry_run, restore_key, cmd_id),
                    daemon=True,
                    name=f"restore-{job_name}",
                )
                restore_thread.start()

            elif cmd_type == "browse_filesystem":
                # Browse is quick, run synchronously
                self._execute_browse_filesystem(payload)
            else:
                print(f"Unknown command: {cmd_type}")
                return

            # Synchronous commands are complete; workers acknowledge themselves.
            if cmd_id and cmd_type not in {"backup", "restore"}:
                self._acknowledge_command(cmd_id)

        except Exception as e:
            print(f"Command {cmd_id} failed: {e}")

    def _run_backup_worker(self, job_data: dict[str, Any], dry_run: bool, job_name: str, cmd_id: int | None) -> None:
        """Worker thread for executing backups without blocking heartbeat."""
        try:
            self.execute_backup(job_data, dry_run=dry_run)
        except Exception as e:
            print(f"Backup worker failed: {e}")
            import traceback

            traceback.print_exc()
        finally:
            if cmd_id:
                self._acknowledge_command(cmd_id)
            # Remove from active jobs when done
            with self._active_jobs_lock:
                self._active_jobs.discard(job_name)

    def _run_restore_worker(self, payload: dict[str, Any], dry_run: bool, restore_key: str, cmd_id: int | None) -> None:
        """Worker thread for executing restores without blocking heartbeat."""
        try:
            self.execute_restore(payload, dry_run=dry_run)
        except Exception as e:
            print(f"Restore worker failed: {e}")
            import traceback

            traceback.print_exc()
        finally:
            if cmd_id:
                self._acknowledge_command(cmd_id)
            # Remove from active jobs when done
            with self._active_jobs_lock:
                self._active_jobs.discard(restore_key)

    def _acknowledge_command(self, command_id: int) -> None:
        """Acknowledge that a command was processed."""
        try:
            client = self._get_client()
            client.post(f"/api/v1/commands/{command_id}/ack")
        except Exception as e:
            print(f"Failed to acknowledge command {command_id}: {e}")

    def _execute_browse_filesystem(self, payload: dict[str, Any]) -> None:
        """Execute filesystem browse command and report results."""
        request_id = payload.get("request_id")
        if not request_id:
            print("[BROWSE] Missing request_id in payload")
            return

        path = payload.get("path", "")
        print(f"[BROWSE] Browsing filesystem at: {path or '(root)'}")

        try:
            entries = []

            if not path:
                # Return root directories - Home and /
                home = Path.home()
                if home.exists():
                    entries.append(
                        {
                            "name": "Home",
                            "path": str(home),
                            "is_dir": True,
                            "size": 0,
                        }
                    )
                entries.append(
                    {
                        "name": "/",
                        "path": "/",
                        "is_dir": True,
                        "size": 0,
                    }
                )
                actual_path = ""
            else:
                # List contents of the specified path
                browse_path = Path(path)
                actual_path = str(browse_path)

                if not browse_path.exists():
                    raise FileNotFoundError(f"Path does not exist: {path}")

                if not browse_path.is_dir():
                    raise NotADirectoryError(f"Path is not a directory: {path}")

                # Use os.scandir for better performance
                dirs = []
                files = []

                try:
                    with os.scandir(str(browse_path)) as scanner:
                        for entry in scanner:
                            try:
                                # Use cached is_dir from scandir (much faster)
                                is_dir = entry.is_dir(follow_symlinks=False)

                                item_entry = {
                                    "name": entry.name,
                                    "path": entry.path,
                                    "is_dir": is_dir,
                                    "size": 0,
                                }

                                if is_dir:
                                    dirs.append(item_entry)
                                else:
                                    # Get file size from cached stat
                                    try:
                                        item_entry["size"] = entry.stat().st_size
                                    except (OSError, PermissionError):
                                        pass
                                    files.append(item_entry)

                                # Stop early if we have enough entries
                                if len(dirs) + len(files) >= 500:
                                    break

                            except (OSError, PermissionError):
                                # Skip items we can't access
                                continue
                except PermissionError:
                    raise PermissionError(f"Permission denied: {path}")

                # Sort directories and files by name, then combine (dirs first)
                dirs.sort(key=lambda x: x["name"].lower())
                files.sort(key=lambda x: x["name"].lower())
                entries = (dirs + files)[:200]

            # Report results to server
            client = self._get_client()
            client.post(
                f"/api/v1/browse/{request_id}/results",
                json={
                    "success": True,
                    "path": actual_path,
                    "entries": entries,
                },
            )

            print(f"[BROWSE] Sent {len(entries)} entries for path: {path or '(root)'}")

        except Exception as e:
            print(f"[BROWSE] Failed to browse {path}: {e}")
            # Report error to server
            try:
                client = self._get_client()
                client.post(
                    f"/api/v1/browse/{request_id}/results",
                    json={
                        "success": False,
                        "path": path,
                        "entries": [],
                        "error": str(e),
                    },
                )
            except Exception as report_err:
                print(f"[BROWSE] Failed to report error: {report_err}")

    def _report_progress(
        self,
        run_id: str,
        status: str | None = None,
        progress_percent: int | None = None,
        current_file: str | None = None,
        bytes_processed: int | None = None,
        files_processed: int | None = None,
        message: str | None = None,
    ) -> None:
        """Report progress update to server."""
        try:
            client = self._get_client()
            client.post(
                "/api/v1/progress",
                json={
                    "run_id": run_id,
                    "status": status,
                    "progress_percent": progress_percent,
                    "current_file": current_file,
                    "bytes_processed": bytes_processed,
                    "files_processed": files_processed,
                    "message": message,
                },
            )
        except Exception as e:
            print(f"Failed to report progress: {e}")

    def _is_smb_path(self, path: str) -> bool:
        return is_smb_path(path)

    def _is_nfs_path(self, path: str) -> bool:
        return is_nfs_path(path)

    def _parse_smb_path(self, path: str) -> tuple[str, str, str]:
        return parse_smb_path(path)

    def _parse_nfs_path(self, path: str) -> tuple[str, str, str]:
        return parse_nfs_path(path)

    def _check_cifs_available(self) -> bool:
        return check_cifs_available()

    def _check_nfs_available(self) -> bool:
        return check_nfs_available()

    def _smb_mount_context(
        self,
        server: str,
        share: str,
        username: str | None = None,
        password: str | None = None,
        domain: str | None = None,
    ) -> Generator[Path, None, None]:
        return smb_mount_context(server, share, username, password, domain, cifs_check=self._check_cifs_available)

    def _nfs_mount_context(self, server: str, export_path: str) -> Generator[Path, None, None]:
        return nfs_mount_context(server, export_path, nfs_check=self._check_nfs_available)

    def _prepare_destination_for_backend(
        self,
        job: dict[str, Any],
        backend_name: str,
    ) -> tuple[str, Any]:
        return prepare_destination(
            job,
            backend_name,
            smb_mount=self._smb_mount_context,
            nfs_mount=self._nfs_mount_context,
        )

    def _prepare_source_for_backend(
        self,
        job: dict[str, Any],
        backend_name: str,
    ) -> tuple[str, Any]:
        """Prepare source path for the backend, mounting SMB/NFS if needed.

        Returns:
            Tuple of (prepared_path, cleanup_context).
            cleanup_context should be used to unmount if not None.
        """
        return prepare_source(
            job,
            backend_name,
            smb_mount=self._smb_mount_context,
            nfs_mount=self._nfs_mount_context,
        )

    def _post_result(self, report: dict[str, Any]) -> None:
        self._get_client().post("/api/v1/results", json=report)

    def execute_backup(self, job: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        return run_backup(
            job,
            dry_run=dry_run,
            on_progress=self._report_progress,
            on_result=self._post_result,
            agent_credentials=(self.client_id, self.client_secret),
        )

    def execute_restore(self, job: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        return run_restore(
            job,
            dry_run=dry_run,
            on_progress=self._report_progress,
            on_result=self._post_result,
            agent_credentials=(self.client_id, self.client_secret),
        )

    def _write_metadata_to_path(
        self,
        repo_path: Path,
        job_name: str,
        run_id: str,
        source_path: str,
        backend_name: str,
        result: Any,
        started_at: datetime,
        finished_at: datetime,
        snapshot_id: str | None,
    ) -> None:
        _runner_write_metadata_to_path(
            repo_path,
            job_name,
            run_id,
            source_path,
            backend_name,
            result,
            started_at,
            finished_at,
            snapshot_id,
            self.client_id,
        )

    def _write_repo_metadata(
        self,
        job: dict[str, Any],
        dest_path: str,
        backend_name: str,
        result: Any,
        started_at: datetime,
        finished_at: datetime,
        snapshot_id: str | None,
    ) -> None:
        _runner_write_repo_metadata(
            job, dest_path, backend_name, result, started_at, finished_at, snapshot_id, self.client_id
        )

    def _write_restore_metadata(
        self,
        source_path: str,
        job_name: str,
        run_id: str,
        result: Any,
        started_at: datetime,
        finished_at: datetime,
        snapshot: str | None,
    ) -> None:
        _runner_write_restore_metadata(
            source_path, job_name, run_id, result, started_at, finished_at, snapshot, self.client_id
        )

    def run(self, heartbeat_interval: int = 60) -> None:
        """Run the agent in daemon mode."""
        self._stop_event.clear()

        # Set up signal handlers (SIGTERM not available on Windows)
        signal.signal(signal.SIGINT, self._handle_signal)
        if sys.platform != "win32":
            signal.signal(signal.SIGTERM, self._handle_signal)

        print(f"Backer agent starting (client_id: {self.client_id})")
        print(f"Connecting to server: {self.server_url}")

        # Start heartbeat thread
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(heartbeat_interval,),
            daemon=True,
        )
        self._heartbeat_thread.start()

        # Main loop - wait for stop signal
        try:
            self._stop_event.wait()
        except KeyboardInterrupt:
            pass

        self.stop()

    def stop(self) -> None:
        """Stop the agent."""
        print("Stopping agent...")
        self._stop_event.set()

        with self._http_client_lock:
            if self._http_client:
                self._http_client.close()
                self._http_client = None

    def _handle_signal(self, signum: int, frame: object) -> None:
        """Handle shutdown signals gracefully.

        Sets stop event to allow current operations to complete
        before exiting. This ensures backup results are reported to the server.
        """
        print(f"\nReceived signal {signum}, initiating graceful shutdown...")
        self._stop_event.set()
        # Don't call sys.exit(0) immediately - let the main loop exit naturally
        # This allows any in-progress backup reporting to complete
