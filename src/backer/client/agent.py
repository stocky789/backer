"""Backer agent - runs on client machines and executes backups."""

import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from backer import __version__
from backer.backends import get_backend
from backer.backends.base import BackupDestination, BackupSource
from backer.core.repo_metadata import RepositoryMetadata


def get_config_dir() -> Path:
    """Get platform-appropriate config directory.

    Linux: /etc/backer (system-wide)
    Windows: %APPDATA%/Backer
    """
    # Check environment variable first (for custom locations)
    env_config = os.environ.get("BACKER_CONFIG_DIR")
    if env_config:
        return Path(env_config)

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Backer"
        return Path.home() / "AppData" / "Roaming" / "Backer"
    else:
        # Linux/Mac: always system-wide
        return Path("/etc/backer")


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
                timeout=35.0,  # Server uses 25s long-polling, need longer timeout
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
        """Background heartbeat loop.

        Uses long-polling: the server holds the heartbeat request until
        a command is available (up to 25s), so we get commands instantly.
        We only sleep briefly between heartbeats to allow quick shutdown.

        The interval parameter is only used on connection errors to avoid
        hammering the server.
        """
        while self._running:
            try:
                result = self.heartbeat()
                # Process any pending commands
                for cmd in result.get("commands", []):
                    self._handle_command(cmd)
            except Exception as e:
                print(f"Heartbeat failed: {e}")
                # On error, wait before retry to avoid hammering server
                for _ in range(5):
                    if not self._running:
                        break
                    time.sleep(1)
                continue

            # Brief pause between heartbeats (server handles the waiting)
            if not self._running:
                break
            time.sleep(1)

    def _handle_command(self, command: dict[str, Any]) -> None:
        """Handle a command from the server."""
        cmd_type = command.get("command_type")
        cmd_id = command.get("id")
        payload = command.get("payload", {})

        print(f"Received command: {cmd_type} (id={cmd_id})")

        try:
            if cmd_type == "backup":
                # Merge command-level fields into payload for backwards compat
                job_data = {**command, **payload}
                dry_run = payload.get("dry_run", False)
                self.execute_backup(job_data, dry_run=dry_run)
            elif cmd_type == "restore":
                dry_run = payload.get("dry_run", False)
                self.execute_restore(payload, dry_run=dry_run)
            elif cmd_type == "browse_filesystem":
                self._execute_browse_filesystem(payload)
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
                    entries.append({
                        "name": "Home",
                        "path": str(home),
                        "is_dir": True,
                        "size": 0,
                    })
                entries.append({
                    "name": "/",
                    "path": "/",
                    "is_dir": True,
                    "size": 0,
                })
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
            client.post("/api/v1/progress", json={
                "run_id": run_id,
                "status": status,
                "progress_percent": progress_percent,
                "current_file": current_file,
                "bytes_processed": bytes_processed,
                "files_processed": files_processed,
                "message": message,
            })
        except Exception as e:
            print(f"Failed to report progress: {e}")

    def _is_smb_path(self, path: str) -> bool:
        """Check if a path is an SMB/UNC path."""
        return path.startswith("//") or path.startswith("\\\\")

    def _is_nfs_path(self, path: str) -> bool:
        """Check if a path is an NFS path (server:/export format)."""
        # NFS paths look like: server:/export/path or 192.168.1.1:/share/path
        # But NOT like /local/path or C:\path
        if path.startswith("/") or path.startswith("\\"):
            return False
        if ":" in path:
            # Check it's not a Windows drive letter (C:)
            parts = path.split(":", 1)
            if len(parts) == 2 and len(parts[0]) > 1:
                # More than one char before colon, likely NFS
                return parts[1].startswith("/")
        return False

    def _parse_smb_path(self, path: str) -> tuple[str, str, str]:
        """Parse an SMB path into (server, share, subpath).

        Examples:
            //192.168.0.254/HomeNetwork/Backer -> (192.168.0.254, HomeNetwork, Backer)
            //server/share -> (server, share, "")
        """
        # Normalize to forward slashes
        path = path.replace("\\", "/").lstrip("/")
        parts = path.split("/")

        server = parts[0] if len(parts) > 0 else ""
        share = parts[1] if len(parts) > 1 else ""
        subpath = "/".join(parts[2:]) if len(parts) > 2 else ""

        return server, share, subpath

    def _parse_nfs_path(self, path: str) -> tuple[str, str, str]:
        """Parse an NFS path into (server, export, subpath).

        Examples:
            192.168.0.254:/exports/backup/data -> (192.168.0.254, /exports/backup, data)
            server:/share -> (server, /share, "")
        """
        # Split on first colon
        parts = path.split(":", 1)
        server = parts[0]
        export_path = parts[1] if len(parts) > 1 else "/"

        # The export is typically the first part, subpath is the rest
        # Common pattern: server:/export/subpath
        # We'll treat the entire path after : as the export initially
        # The actual export point is determined by the NFS server
        return server, export_path, ""

    def _check_cifs_available(self) -> bool:
        """Check if cifs-utils is installed for SMB mounting."""
        try:
            result = subprocess.run(
                ["mount.cifs", "-V"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _check_nfs_available(self) -> bool:
        """Check if NFS mount tools are installed."""
        try:
            # Check for mount.nfs (provided by nfs-common on Debian/Ubuntu)
            result = subprocess.run(
                ["mount.nfs", "-V"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            # mount.nfs might not have -V, try checking if it exists
            import shutil
            return shutil.which("mount.nfs") is not None

    def _rclone_obscure_password(self, password: str) -> str | None:
        """Obscure a password for use with rclone on-the-fly backends.

        Rclone requires passwords to be "obscured" when used in connection strings.
        This runs 'rclone obscure <password>' to get the obscured form.
        """
        try:
            from backer.tools.manager import get_tool_manager
            tool_manager = get_tool_manager()

            # Use ensure_installed to auto-download rclone if not present
            try:
                rclone_path = tool_manager.ensure_installed("rclone")
            except Exception as e:
                print(f"[SMB] Warning: Failed to get rclone: {e}")
                return None

            result = subprocess.run(
                [str(rclone_path), "obscure", password],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:
                return result.stdout.strip()
            else:
                print(f"[SMB] Warning: rclone obscure failed: {result.stderr}")
                return None
        except Exception as e:
            print(f"[SMB] Warning: Failed to obscure password: {e}")
            return None

    @contextmanager
    def _smb_mount_context(
        self,
        server: str,
        share: str,
        username: str | None = None,
        password: str | None = None,
        domain: str | None = None,
    ) -> Generator[Path, None, None]:
        """Context manager that mounts an SMB share and yields the mount path.

        Used for restic/kopia on Linux, which need a local filesystem path.
        Requires cifs-utils to be installed.
        """
        # Check if cifs-utils is available
        if not self._check_cifs_available():
            raise RuntimeError(
                "cifs-utils not installed. Install it with:\n"
                "  Debian/Ubuntu: sudo apt install cifs-utils\n"
                "  RHEL/Fedora: sudo dnf install cifs-utils\n"
                "  Arch: sudo pacman -S cifs-utils"
            )

        mount_point = Path(tempfile.mkdtemp(prefix="backer_smb_"))

        try:
            # Build mount command
            smb_url = f"//{server}/{share}"
            cmd = ["mount", "-t", "cifs", smb_url, str(mount_point)]

            # Build mount options
            opts = ["rw"]
            if username:
                opts.append(f"username={username}")
            if password:
                opts.append(f"password={password}")
            if domain:
                opts.append(f"domain={domain}")
            if not username and not password:
                opts.append("guest")

            cmd.extend(["-o", ",".join(opts)])

            print(f"[SMB] Mounting {smb_url} to {mount_point}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode != 0:
                error_msg = result.stderr.strip()
                if "Permission denied" in error_msg:
                    raise RuntimeError(f"SMB mount permission denied. Check credentials for //{server}/{share}")
                elif "No such file or directory" in error_msg:
                    raise RuntimeError(f"SMB share not found: //{server}/{share}")
                elif "Connection refused" in error_msg or "Host is down" in error_msg:
                    raise RuntimeError(f"Cannot connect to SMB server: {server}")
                else:
                    raise RuntimeError(f"Failed to mount SMB share: {error_msg}")

            print("[SMB] Mounted successfully")
            yield mount_point

        finally:
            # Unmount
            print(f"[SMB] Unmounting {mount_point}")
            try:
                subprocess.run(["umount", str(mount_point)], capture_output=True, timeout=30)
            except Exception as e:
                print(f"[SMB] Warning: unmount failed: {e}")

            # Clean up mount point directory
            try:
                mount_point.rmdir()
            except Exception:
                pass

    @contextmanager
    def _nfs_mount_context(
        self,
        server: str,
        export_path: str,
    ) -> Generator[Path, None, None]:
        """Context manager that mounts an NFS export and yields the mount path.

        Used for restic/kopia on Linux, which need a local filesystem path.
        Requires nfs-common (Debian/Ubuntu) or nfs-utils (RHEL/Fedora) to be installed.
        """
        # Check if NFS tools are available
        if not self._check_nfs_available():
            raise RuntimeError(
                "NFS mount tools not installed. Install with:\n"
                "  Debian/Ubuntu: sudo apt install nfs-common\n"
                "  RHEL/Fedora: sudo dnf install nfs-utils\n"
                "  Arch: sudo pacman -S nfs-utils"
            )

        mount_point = Path(tempfile.mkdtemp(prefix="backer_nfs_"))

        try:
            # Build NFS mount command
            nfs_url = f"{server}:{export_path}"
            cmd = ["mount", "-t", "nfs", nfs_url, str(mount_point)]

            # Add common NFS mount options for reliability
            cmd.extend(["-o", "rw,soft,timeo=30,retrans=3"])

            print(f"[NFS] Mounting {nfs_url} to {mount_point}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

            if result.returncode != 0:
                error_msg = result.stderr.strip()
                if "Permission denied" in error_msg or "access denied" in error_msg.lower():
                    raise RuntimeError(f"NFS mount permission denied: {nfs_url}")
                elif "No such file or directory" in error_msg:
                    raise RuntimeError(f"NFS export not found: {nfs_url}")
                elif "Connection refused" in error_msg or "Host is down" in error_msg:
                    raise RuntimeError(f"Cannot connect to NFS server: {server}")
                elif "not responding" in error_msg.lower():
                    raise RuntimeError(f"NFS server not responding: {server}")
                else:
                    raise RuntimeError(f"Failed to mount NFS export: {error_msg}")

            print("[NFS] Mounted successfully")
            yield mount_point

        finally:
            # Unmount
            print(f"[NFS] Unmounting {mount_point}")
            try:
                subprocess.run(["umount", str(mount_point)], capture_output=True, timeout=30)
            except Exception as e:
                print(f"[NFS] Warning: unmount failed: {e}")

            # Clean up mount point directory
            try:
                mount_point.rmdir()
            except Exception:
                pass

    def _prepare_destination_for_backend(
        self,
        job: dict[str, Any],
        backend_name: str,
    ) -> tuple[str, Any]:
        """Prepare the destination path for the backend.

        On Linux, SMB and NFS paths need special handling:
        - For rclone with SMB: Use on-the-fly SMB backend config
        - For rclone with NFS: Mount the export first
        - For restic/kopia: Mount the share/export first

        Returns:
            Tuple of (destination_path, cleanup_context_or_none)
        """
        dest_path = job.get("destination_path", "")

        # Windows can use UNC paths directly
        if sys.platform == "win32":
            return dest_path, None

        # Handle SMB paths
        if self._is_smb_path(dest_path):
            return self._prepare_smb_destination(job, backend_name, dest_path)

        # Check if NFS credentials were passed (job linked to NFS repository)
        # This takes priority over parsing dest_path as NFS, because the server
        # provides the actual NFS export separately from the subpath
        nfs_server = job.get("nfs_server")
        nfs_export = job.get("nfs_export")
        if nfs_server and nfs_export:
            # Server provided NFS export - mount the export and calculate subpath
            # dest_path will be like: server:/export/path/Agents/jobname
            # nfs_export is the actual mountable export: /export/path
            # We need to extract the subpath after the export
            print(f"[NFS] Using NFS repository: {nfs_server}:{nfs_export}")

            # Calculate subpath: everything in dest_path after the export
            # dest_path format: "server:/export/subpath" or just "/local/path"
            subpath = ""
            if self._is_nfs_path(dest_path):
                # Parse the full destination to get the path portion
                _, full_path, _ = self._parse_nfs_path(dest_path)
                # Remove the export prefix to get subpath
                if full_path.startswith(nfs_export):
                    subpath = full_path[len(nfs_export):].lstrip("/")
                else:
                    # Export doesn't match - maybe path format differs, use full path as subpath
                    subpath = full_path.lstrip("/")

            ctx = self._nfs_mount_context(server=nfs_server, export_path=nfs_export)
            mount_path = ctx.__enter__()
            full_path = str(mount_path / subpath) if subpath else str(mount_path)
            print(f"[NFS] Using mounted path: {full_path}")
            return full_path, ctx

        # Handle NFS paths (without server-provided credentials)
        if self._is_nfs_path(dest_path):
            return self._prepare_nfs_destination(job, backend_name, dest_path)

        # Local path, use as-is
        return dest_path, None

    def _prepare_smb_destination(
        self,
        job: dict[str, Any],
        backend_name: str,
        dest_path: str,
    ) -> tuple[str, Any]:
        """Prepare SMB destination path for the backend."""
        server, share, subpath = self._parse_smb_path(dest_path)

        # Get credentials from job (passed by server)
        smb_username = job.get("smb_username")
        smb_password = job.get("smb_password")
        smb_domain = job.get("smb_domain")

        if backend_name in ("rclone", "restic", "kopia"):
            # All backends use mounted filesystem path for SMB on Linux
            # (rclone's on-the-fly SMB has compatibility issues with some NAS devices)
            print(f"[SMB] Mounting share for {backend_name} backend")
            ctx = self._smb_mount_context(
                server=server,
                share=share,
                username=smb_username,
                password=smb_password,
                domain=smb_domain,
            )
            mount_path = ctx.__enter__()
            full_path = str(mount_path / subpath) if subpath else str(mount_path)
            print(f"[SMB] Using mounted path: {full_path}")
            return full_path, ctx

        else:
            # Unknown backend, try using path as-is
            print(f"[SMB] Warning: Unknown backend '{backend_name}', using path as-is")
            return dest_path, None

    def _prepare_nfs_destination(
        self,
        job: dict[str, Any],
        backend_name: str,
        dest_path: str,
    ) -> tuple[str, Any]:
        """Prepare NFS destination path for the backend."""
        server, export_path, subpath = self._parse_nfs_path(dest_path)

        if backend_name == "rclone":
            # For rclone with NFS, we need to mount first as rclone doesn't have
            # native NFS support (unlike SMB). Could use SFTP if SSH is available,
            # but mounting is more reliable.
            print("[NFS] Mounting NFS export for rclone backend")
            ctx = self._nfs_mount_context(server=server, export_path=export_path)
            mount_path = ctx.__enter__()
            full_path = str(mount_path / subpath) if subpath else str(mount_path)
            print(f"[NFS] Using mounted path: {full_path}")
            return full_path, ctx

        elif backend_name in ("restic", "kopia"):
            # restic and kopia need a mounted filesystem path
            print(f"[NFS] Mounting NFS export for {backend_name} backend")
            ctx = self._nfs_mount_context(server=server, export_path=export_path)
            mount_path = ctx.__enter__()
            full_path = str(mount_path / subpath) if subpath else str(mount_path)
            print(f"[NFS] Using mounted path: {full_path}")
            return full_path, ctx

        else:
            # Unknown backend, try mounting anyway
            print(f"[NFS] Warning: Unknown backend '{backend_name}', mounting NFS export")
            ctx = self._nfs_mount_context(server=server, export_path=export_path)
            mount_path = ctx.__enter__()
            full_path = str(mount_path / subpath) if subpath else str(mount_path)
            return full_path, ctx

    def execute_backup(
        self,
        job: dict[str, Any],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Execute a backup job."""
        # Use run_id from command payload if provided, otherwise generate one
        run_id = job.get("run_id") or datetime.now().strftime("%Y%m%d_%H%M%S")
        started_at = datetime.now()
        job_name = job.get("job_name", "unknown")
        backend_name = job.get("backend", "rclone")

        print(f"[BACKUP] Starting job '{job_name}' with backend '{backend_name}'")
        print(f"[BACKUP] Source: {job.get('source_path')}")
        print(f"[BACKUP] Destination: {job.get('destination_path')}")

        # Report that we're starting
        self._report_progress(
            run_id=run_id,
            status="running",
            progress_percent=0,
            message="Initializing backup...",
        )

        smb_cleanup_ctx = None
        try:
            backend_options = job.get("backend_options", {})

            # Log backend options (without password)
            safe_options = {k: v for k, v in backend_options.items() if k != "password"}
            if "password" in backend_options:
                safe_options["password"] = "***"
            print(f"[BACKUP] Backend options: {safe_options}")

            backend = get_backend(
                backend_name,  # rclone default (rsync not supported for agents)
                backend_options,
            )

            print("[BACKUP] Checking backend availability...")
            available, message = backend.check_available()
            if not available:
                print(f"[BACKUP] Backend not available: {message}")
                raise RuntimeError(f"Backend not available: {message}")
            print(f"[BACKUP] Backend ready: {message}")

            # Prepare destination path (handles SMB on Linux)
            print(f"[BACKUP] Preparing destination path for {backend_name} backend...")
            dest_path, smb_cleanup_ctx = self._prepare_destination_for_backend(job, backend_name)
            print(f"[BACKUP] Using destination: {dest_path}")

            self._report_progress(
                run_id=run_id,
                status="running",
                progress_percent=5,
                message="Backend ready, starting transfer...",
            )

            source = BackupSource(
                path=Path(job["source_path"]).expanduser(),
                excludes=job.get("excludes", []),
            )

            destination = BackupDestination(path=dest_path)

            # Create progress callback for backends that support it
            def progress_callback(
                bytes_done: int = 0,
                files_done: int = 0,
                current_file: str = "",
                total_bytes: int = 0,
            ) -> None:
                # Calculate percentage (leave 5% for init, 5% for finish)
                percent = 5
                if total_bytes > 0:
                    percent = 5 + int((bytes_done / total_bytes) * 90)
                elif files_done > 0:
                    # Estimate based on files if no byte info
                    percent = min(5 + files_done, 95)

                self._report_progress(
                    run_id=run_id,
                    status="running",
                    progress_percent=percent,
                    current_file=current_file[:200] if current_file else None,
                    bytes_processed=bytes_done,
                    files_processed=files_done,
                )

            # Check if backend supports progress callback
            supports_progress = (
                hasattr(backend.backup, '__code__') and
                'progress_callback' in backend.backup.__code__.co_varnames
            )

            print(f"[BACKUP] Executing backup: {source.path} -> {dest_path}")
            result = backend.backup(
                source=source,
                destination=destination,
                dry_run=dry_run,
                progress_callback=progress_callback if supports_progress else None,
            )

            finished_at = datetime.now()

            # Log the result
            if result.success:
                print(f"[BACKUP] Job '{job_name}' completed successfully")
                print(f"[BACKUP] Transferred: {result.bytes_transferred} bytes, {result.files_transferred} files")
            else:
                print(f"[BACKUP] Job '{job_name}' completed with errors")
                print(f"[BACKUP] Return code: {result.return_code}")
                if result.errors:
                    print(f"[BACKUP] Errors: {result.errors[:5]}")  # First 5 errors
                if result.output:
                    print(f"[BACKUP] Output (last 1000 chars): {result.output[-1000:]}")

            self._report_progress(
                run_id=run_id,
                status="finishing",
                progress_percent=95,
                message="Finalizing backup...",
            )

            # Report result to server
            # Extract snapshot_id from backend metadata (for kopia/restic)
            snapshot_id = None
            if hasattr(result, "metadata") and result.metadata:
                snapshot_id = result.metadata.get("snapshot_id")
            if snapshot_id:
                print(f"[BACKUP] Captured snapshot ID: {snapshot_id}")

            report = {
                "run_id": run_id,
                "job_name": job_name,
                "client_id": self.client_id,
                "success": result.success,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "bytes_transferred": result.bytes_transferred,
                "files_transferred": result.files_transferred,
                "errors": result.errors,
                "output": result.output[:5000],
                "snapshot_id": snapshot_id,
            }

            try:
                client = self._get_client()
                client.post("/api/v1/results", json=report)
            except Exception as e:
                print(f"Failed to report result: {e}")

            # Write metadata to repository for discovery (only on success)
            if result.success:
                original_dest = job.get("destination_path", "")
                self._write_repo_metadata(
                    job=job,
                    dest_path=original_dest,
                    backend_name=backend_name,
                    result=result,
                    started_at=started_at,
                    finished_at=finished_at,
                    snapshot_id=snapshot_id,
                )

            return report

        except Exception as e:
            import traceback

            finished_at = datetime.now()
            error_msg = str(e)
            error_trace = traceback.format_exc()

            print(f"[BACKUP] Job '{job_name}' FAILED: {error_msg}")
            print(f"[BACKUP] Traceback:\n{error_trace}")

            self._report_progress(
                run_id=run_id,
                status="failed",
                progress_percent=0,
                message=error_msg[:200],
            )

            report = {
                "run_id": run_id,
                "job_name": job_name,
                "client_id": self.client_id,
                "success": False,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "bytes_transferred": 0,
                "files_transferred": 0,
                "errors": [error_msg],
                "output": error_trace[:5000],
            }

            try:
                client = self._get_client()
                client.post("/api/v1/results", json=report)
            except Exception as report_err:
                print(f"[BACKUP] Failed to report error to server: {report_err}")

            return report

        finally:
            # Clean up SMB mount if used
            if smb_cleanup_ctx is not None:
                try:
                    smb_cleanup_ctx.__exit__(None, None, None)
                except Exception as cleanup_err:
                    print(f"[SMB] Cleanup error: {cleanup_err}")

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
        source_path = job.get("source_path", "")

        # Check if NFS credentials were passed (job linked to NFS repository)
        # This takes priority over parsing source_path as NFS, because the server
        # provides the actual NFS export separately from the subpath
        nfs_server = job.get("nfs_server")
        nfs_export = job.get("nfs_export")
        if nfs_server and nfs_export:
            # Server provided NFS export - mount the export and calculate subpath
            # source_path will be like: server:/export/path/Agents/jobname
            # nfs_export is the actual mountable export: /export/path
            # We need to extract the subpath after the export
            print(f"[RESTORE] Using NFS repository: {nfs_server}:{nfs_export}")

            # Calculate subpath: everything in source_path after the export
            subpath = ""
            if self._is_nfs_path(source_path):
                # Parse the full source to get the path portion
                _, full_path, _ = self._parse_nfs_path(source_path)
                # Remove the export prefix to get subpath
                if full_path.startswith(nfs_export):
                    subpath = full_path[len(nfs_export):].lstrip("/")
                else:
                    # Export doesn't match - maybe path format differs, use full path as subpath
                    subpath = full_path.lstrip("/")

            ctx = self._nfs_mount_context(server=nfs_server, export_path=nfs_export)
            mount_path = ctx.__enter__()
            full_path = str(mount_path / subpath) if subpath else str(mount_path)
            print(f"[RESTORE] Using mounted path: {full_path}")
            return full_path, ctx

        # Check for NFS path (server:/export format) without explicit credentials
        if self._is_nfs_path(source_path):
            print(f"[RESTORE] Detected NFS source path: {source_path}")
            return self._prepare_nfs_source(job, backend_name, source_path)

        # Check for SMB path (//server/share or \\server\share format)
        if self._is_smb_path(source_path):
            print(f"[RESTORE] Detected SMB source path: {source_path}")
            return self._prepare_smb_source(job, backend_name, source_path)

        # Local path, use as-is
        return source_path, None

    def _prepare_smb_source(
        self,
        job: dict[str, Any],
        backend_name: str,
        source_path: str,
    ) -> tuple[str, Any]:
        """Prepare SMB source path for restore."""
        server, share, subpath = self._parse_smb_path(source_path)

        # Get credentials from job (passed by server)
        smb_username = job.get("smb_username")
        smb_password = job.get("smb_password")
        smb_domain = job.get("smb_domain")

        if backend_name == "rclone":
            # rclone on-the-fly SMB backend format
            smb_opts = [f"host={server}", f"share={share}"]
            if smb_username:
                smb_opts.append(f"user={smb_username}")
            if smb_password:
                obscured_pass = self._rclone_obscure_password(smb_password)
                if obscured_pass:
                    smb_opts.append(f"pass={obscured_pass}")
                else:
                    print("[RESTORE] Warning: Could not obscure password, trying plaintext")
                    smb_opts.append(f"pass={smb_password}")
            if smb_domain:
                smb_opts.append(f"domain={smb_domain}")

            if subpath and subpath.strip():
                rclone_path = f":smb,{','.join(smb_opts)}:/{subpath}"
            else:
                rclone_path = f":smb,{','.join(smb_opts)}:/"

            print(f"[RESTORE] Using rclone SMB backend for //{server}/{share}/{subpath or ''}")
            return rclone_path, None

        elif backend_name in ("restic", "kopia"):
            # restic and kopia need a mounted filesystem path
            print(f"[RESTORE] Mounting SMB share for {backend_name} backend")
            ctx = self._smb_mount_context(
                server=server,
                share=share,
                username=smb_username,
                password=smb_password,
                domain=smb_domain,
            )
            mount_path = ctx.__enter__()
            full_path = str(mount_path / subpath) if subpath else str(mount_path)
            print(f"[RESTORE] Using mounted path: {full_path}")
            return full_path, ctx

        else:
            print(f"[RESTORE] Warning: Unknown backend '{backend_name}', using path as-is")
            return source_path, None

    def _prepare_nfs_source(
        self,
        job: dict[str, Any],
        backend_name: str,
        source_path: str,
    ) -> tuple[str, Any]:
        """Prepare NFS source path for restore.

        Note: This is called when nfs_server/nfs_export were NOT provided explicitly.
        It parses the full NFS path and extracts server, export, and subpath.
        """
        server, export_path, subpath = self._parse_nfs_path(source_path)

        # All backends need mounted path for NFS
        print(f"[RESTORE] Mounting NFS export {server}:{export_path} for {backend_name} backend")
        ctx = self._nfs_mount_context(server=server, export_path=export_path)
        mount_path = ctx.__enter__()
        full_path = str(mount_path / subpath) if subpath else str(mount_path)
        print(f"[RESTORE] Using mounted path: {full_path}")
        return full_path, ctx

    def execute_restore(
        self,
        job: dict[str, Any],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Execute a restore job."""
        run_id = job.get("run_id") or f"restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        started_at = datetime.now()
        job_name = job.get("job_name", "unknown")
        backend_name = job.get("backend", "rclone")

        print(f"[RESTORE] Starting restore for job '{job_name}' with backend '{backend_name}'")
        print(f"[RESTORE] Source (backup repo): {job.get('source_path')}")
        print(f"[RESTORE] Destination: {job.get('destination_path')}")

        # Report that we're starting restore
        self._report_progress(
            run_id=run_id,
            status="running",
            progress_percent=0,
            message="Initializing restore...",
        )

        mount_cleanup_ctx = None
        try:
            backend_options = job.get("backend_options", {})

            # Log backend options (without password)
            safe_options = {k: v for k, v in backend_options.items() if k != "password"}
            if "password" in backend_options:
                safe_options["password"] = "***"
            print(f"[RESTORE] Backend options: {safe_options}")

            backend = get_backend(
                backend_name,
                backend_options,
            )

            print("[RESTORE] Checking backend availability...")
            available, message = backend.check_available()
            if not available:
                print(f"[RESTORE] Backend not available: {message}")
                raise RuntimeError(f"Backend not available: {message}")
            print(f"[RESTORE] Backend ready: {message}")

            # Prepare source path (handles SMB/NFS mounting)
            print(f"[RESTORE] Preparing source path for {backend_name} backend...")
            source_path, mount_cleanup_ctx = self._prepare_source_for_backend(job, backend_name)
            print(f"[RESTORE] Prepared source path: {source_path}")

            self._report_progress(
                run_id=run_id,
                status="running",
                progress_percent=5,
                message="Backend ready, starting restore...",
            )

            source = BackupDestination(path=source_path)
            destination = Path(job["destination_path"])

            # Handle clean restore - wipe destination directory first
            clean_restore = job.get("clean_restore", False)
            if clean_restore and not dry_run:
                print(f"[RESTORE] Clean restore enabled - wiping destination: {destination}")
                self._report_progress(
                    run_id=run_id,
                    status="running",
                    progress_percent=3,
                    message="Clean restore: removing existing files...",
                )
                try:
                    if destination.exists():
                        # Remove all contents but keep the directory
                        for item in destination.iterdir():
                            if item.is_dir():
                                shutil.rmtree(item)
                            else:
                                item.unlink()
                        print("[RESTORE] Wiped destination directory contents")
                    else:
                        # Create the directory if it doesn't exist
                        destination.mkdir(parents=True, exist_ok=True)
                        print("[RESTORE] Created destination directory")
                except Exception as wipe_err:
                    print(f"[RESTORE] Warning: Failed to wipe destination: {wipe_err}")
                    # Continue with restore anyway - better to have extra files than fail

            # Pass original_source_path for kopia/restic snapshot lookup
            original_source_path = job.get("original_source_path")
            if original_source_path:
                print(f"[RESTORE] Original source path for snapshot lookup: {original_source_path}")

            result = backend.restore(
                source=source,
                destination=destination,
                snapshot=job.get("snapshot"),
                dry_run=dry_run,
                original_source_path=original_source_path,
            )

            finished_at = datetime.now()

            self._report_progress(
                run_id=run_id,
                status="finishing",
                progress_percent=95,
                message="Finalizing restore...",
            )

            # Report result to server
            report = {
                "run_id": run_id,
                "job_name": f"restore:{job_name}",
                "client_id": self.client_id,
                "success": result.success,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "bytes_transferred": getattr(result, "bytes_transferred", 0),
                "files_transferred": result.files_transferred,
                "errors": result.errors,
                "output": getattr(result, "output", "")[:5000],
            }

            try:
                client = self._get_client()
                client.post("/api/v1/results", json=report)
            except Exception as e:
                print(f"Failed to report restore result: {e}")

            return report

        except Exception as e:
            finished_at = datetime.now()

            self._report_progress(
                run_id=run_id,
                status="failed",
                progress_percent=0,
                message=str(e)[:200],
            )

            report = {
                "run_id": run_id,
                "job_name": f"restore:{job_name}",
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

        finally:
            # Clean up any mounted paths
            if mount_cleanup_ctx:
                try:
                    print("[RESTORE] Cleaning up mounted path...")
                    mount_cleanup_ctx.__exit__(None, None, None)
                    print("[RESTORE] Mount cleanup complete")
                except Exception as cleanup_err:
                    print(f"[RESTORE] Cleanup error: {cleanup_err}")

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
        """Write backup metadata to the repository for discovery.

        Enables a new Backer server to discover existing backups when pointed
        to a repository. For SMB shares on Linux, mounts the share first.
        """
        try:
            print(f"[METADATA] Writing metadata to repository: {dest_path}")

            job_name = job.get("job_name", "unknown")
            run_id = job.get("run_id", "unknown")
            source_path = job.get("source_path", "")

            # For SMB paths on Linux, we need to mount first
            if sys.platform != "win32" and self._is_smb_path(dest_path):
                server, share, subpath = self._parse_smb_path(dest_path)
                smb_username = job.get("smb_username")
                smb_password = job.get("smb_password")
                smb_domain = job.get("smb_domain")

                print(f"[METADATA] Mounting SMB share //{server}/{share} for metadata")
                with self._smb_mount_context(
                    server=server,
                    share=share,
                    username=smb_username,
                    password=smb_password,
                    domain=smb_domain,
                ) as mount_point:
                    if subpath:
                        local_path = mount_point / subpath
                    else:
                        local_path = mount_point
                    self._write_metadata_to_path(
                        local_path, job_name, run_id, source_path, backend_name,
                        result, started_at, finished_at, snapshot_id,
                    )
            else:
                # Local path or Windows UNC path
                self._write_metadata_to_path(
                    Path(dest_path), job_name, run_id, source_path, backend_name,
                    result, started_at, finished_at, snapshot_id,
                )

            print(f"[METADATA] Successfully wrote metadata to: {dest_path}")

        except Exception as e:
            # Don't fail the backup just because metadata writing failed
            print(f"[METADATA] Warning - failed to write metadata: {e}")

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
        """Write metadata files to a local path."""
        repo = RepositoryMetadata(repo_path)

        # Initialize repository metadata if needed
        if not repo.is_initialized():
            print(f"[METADATA] Initializing metadata directory at {repo.metadata_dir}")
            repo.initialize()

        # Save agent information
        repo.save_agent(
            agent_id=self.client_id,
            agent_data={
                "hostname": socket.gethostname(),
                "platform": sys.platform,
                "os_info": f"{platform.system()} {platform.release()}",
            },
        )

        # Save job configuration
        repo.save_job(
            job_name=job_name,
            job_config={
                "source_path": source_path,
                "backend": backend_name,
                "client_id": self.client_id,
            },
        )

        # Save run record
        run_data = {
            "status": "success" if result.success else "failed",
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "bytes_transferred": result.bytes_transferred,
            "files_transferred": result.files_transferred,
            "snapshot_id": snapshot_id,
            "agent_id": self.client_id,
            "hostname": socket.gethostname(),
        }
        repo.save_job_run(job_name, run_id, run_data)

        # For restic/kopia, also save snapshot metadata
        if snapshot_id and backend_name in ("restic", "kopia"):
            repo.save_snapshot(
                snapshot_id=snapshot_id,
                snapshot_data={
                    "job_name": job_name,
                    "run_id": run_id,
                    "hostname": socket.gethostname(),
                    "paths": [source_path],
                    "time": finished_at.isoformat(),
                    "backend": backend_name,
                },
            )

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
        """Handle shutdown signals gracefully.

        Sets running flag to false to allow current operations to complete
        before exiting. This ensures backup results are reported to the server.
        """
        print(f"\nReceived signal {signum}, initiating graceful shutdown...")
        self._running = False
        # Don't call sys.exit(0) immediately - let the main loop exit naturally
        # This allows any in-progress backup reporting to complete
