"""Backer agent - runs on client machines and executes backups."""

import os
import platform
import signal
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from backer import __version__
from backer.backends import get_backend
from backer.backends.base import BackupDestination, BackupSource


def get_config_dir() -> Path:
    """Get platform-appropriate config directory."""
    if sys.platform == "win32":
        # Use APPDATA on Windows
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Backer"
        return Path.home() / "AppData" / "Roaming" / "Backer"
    else:
        # Use XDG config on Linux/Mac
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        if xdg_config:
            return Path(xdg_config) / "backer"
        return Path.home() / ".config" / "backer"


def get_data_dir() -> Path:
    """Get platform-appropriate data directory."""
    if sys.platform == "win32":
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            return Path(localappdata) / "Backer"
        return Path.home() / "AppData" / "Local" / "Backer"
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            return Path(xdg_data) / "backer"
        return Path.home() / ".local" / "share" / "backer"


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
        self.config_path = config_path or get_config_dir() / "agent.yaml"
        self.hostname = socket.gethostname()

        self._running = False
        self._heartbeat_thread: threading.Thread | None = None
        self._http_client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        """Get or create HTTP client."""
        if self._http_client is None:
            auth = None
            if self.client_id and self.client_secret:
                auth = (self.client_id, self.client_secret)
            self._http_client = httpx.Client(
                base_url=self.server_url,
                auth=auth,
                timeout=30.0,
            )
        return self._http_client

    def register(self) -> tuple[str, str]:
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
            },
        )
        response.raise_for_status()
        data = response.json()

        self.client_id = data["client_id"]
        self.client_secret = data["client_secret"]

        # Save credentials
        self._save_credentials()

        # Recreate client with auth
        self._http_client = None

        return self.client_id, self.client_secret

    def _save_credentials(self) -> None:
        """Save client credentials to config file."""
        import yaml

        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        config = {
            "server_url": self.server_url,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        with open(self.config_path, "w") as f:
            yaml.dump(config, f)

        # Secure the file (skip on Windows where chmod doesn't work the same)
        if sys.platform != "win32":
            self.config_path.chmod(0o600)

    @classmethod
    def from_config(cls, config_path: Path | None = None) -> "BackerAgent":
        """Load agent from saved configuration."""
        import yaml

        if config_path is None:
            config_path = get_config_dir() / "agent.yaml"

        if not config_path.exists():
            raise FileNotFoundError(f"Agent config not found: {config_path}")

        with open(config_path) as f:
            config = yaml.safe_load(f)

        return cls(
            server_url=config["server_url"],
            client_id=config.get("client_id"),
            client_secret=config.get("client_secret"),
            config_path=config_path,
        )

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
        """Background heartbeat loop."""
        while self._running:
            try:
                result = self.heartbeat()
                # Process any pending commands
                for cmd in result.get("commands", []):
                    self._handle_command(cmd)
            except Exception as e:
                print(f"Heartbeat failed: {e}")

            # Sleep in small increments to allow quick shutdown
            for _ in range(interval):
                if not self._running:
                    break
                time.sleep(1)

    def _handle_command(self, command: dict[str, Any]) -> None:
        """Handle a command from the server."""
        cmd_type = command.get("command")
        cmd_id = command.get("id")
        payload = command.get("payload", {})

        print(f"Received command: {cmd_type} (id={cmd_id})")

        try:
            if cmd_type == "backup":
                # Merge command-level fields into payload for backwards compat
                job_data = {**command, **payload}
                self.execute_backup(job_data)
            elif cmd_type == "restore":
                self.execute_restore(payload)
            else:
                print(f"Unknown command: {cmd_type}")
                return

            # Acknowledge command was processed
            if cmd_id:
                self._acknowledge_command(cmd_id)

        except Exception as e:
            print(f"Command {cmd_id} failed: {e}")

    def _acknowledge_command(self, command_id: int) -> None:
        """Acknowledge that a command was processed."""
        try:
            client = self._get_client()
            client.post(f"/api/v1/commands/{command_id}/ack")
        except Exception as e:
            print(f"Failed to acknowledge command {command_id}: {e}")

    def execute_backup(
        self,
        job: dict[str, Any],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Execute a backup job."""
        # Use run_id from command payload if provided, otherwise generate one
        run_id = job.get("run_id") or datetime.now().strftime("%Y%m%d_%H%M%S")
        started_at = datetime.now()

        try:
            backend = get_backend(
                job.get("backend", "rsync"),
                job.get("backend_options", {}),
            )

            available, message = backend.check_available()
            if not available:
                raise RuntimeError(f"Backend not available: {message}")

            source = BackupSource(
                path=Path(job["source_path"]).expanduser(),
                excludes=job.get("excludes", []),
            )

            destination = BackupDestination(path=job["destination_path"])

            result = backend.backup(
                source=source,
                destination=destination,
                dry_run=dry_run,
            )

            finished_at = datetime.now()

            # Report result to server
            report = {
                "run_id": run_id,
                "job_name": job.get("job_name", "unknown"),
                "client_id": self.client_id,
                "success": result.success,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "bytes_transferred": result.bytes_transferred,
                "files_transferred": result.files_transferred,
                "errors": result.errors,
                "output": result.output[:5000],
            }

            try:
                client = self._get_client()
                client.post("/api/v1/results", json=report)
            except Exception as e:
                print(f"Failed to report result: {e}")

            return report

        except Exception as e:
            finished_at = datetime.now()
            report = {
                "run_id": run_id,
                "job_name": job.get("job_name", "unknown"),
                "client_id": self.client_id,
                "success": False,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "bytes_transferred": 0,
                "files_transferred": 0,
                "errors": [str(e)],
                "output": "",
            }

            try:
                client = self._get_client()
                client.post("/api/v1/results", json=report)
            except Exception:
                pass

            return report

    def execute_restore(
        self,
        job: dict[str, Any],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Execute a restore job."""
        backend = get_backend(
            job.get("backend", "rsync"),
            job.get("backend_options", {}),
        )

        source = BackupDestination(path=job["source_path"])
        destination = Path(job["destination_path"])

        result = backend.restore(
            source=source,
            destination=destination,
            snapshot=job.get("snapshot"),
            dry_run=dry_run,
        )

        return {
            "success": result.success,
            "errors": result.errors,
            "files_transferred": result.files_transferred,
        }

    def run(self, heartbeat_interval: int = 60) -> None:
        """Run the agent in daemon mode."""
        self._running = True

        # Set up signal handlers
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        print(f"Backer agent starting (client_id: {self.client_id})")
        print(f"Connecting to server: {self.server_url}")

        # Start heartbeat thread
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(heartbeat_interval,),
            daemon=True,
        )
        self._heartbeat_thread.start()

        # Main loop - just keep the process alive
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

        self.stop()

    def stop(self) -> None:
        """Stop the agent."""
        print("Stopping agent...")
        self._running = False

        if self._http_client:
            self._http_client.close()
            self._http_client = None

    def _handle_signal(self, signum: int, frame: object) -> None:
        """Handle shutdown signals."""
        print(f"\nReceived signal {signum}")
        self._running = False
        sys.exit(0)
