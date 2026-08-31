"""SMB/NFS path parsing and mount helpers."""

import logging
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def get_subprocess_flags() -> int:
    """Get subprocess creation flags to hide console window on Windows."""
    if sys.platform == "win32":
        return subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0x08000000
    return 0


class SMBConnectionManager:
    """Manages persistent SMB connections for Windows agents.

    This prevents Error 1219 by reusing existing connections and properly
    managing credentials through Windows Credential Manager.
    """

    def __init__(self):
        self._connections: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def connect(
        self,
        server: str,
        share: str,
        username: str | None,
        password: str | None,
        domain: str | None = None,
    ) -> bool:
        """Connect to SMB share, reusing existing connection if credentials match.

        Args:
            server: SMB server hostname or IP
            share: Share name
            username: Username for authentication
            password: Password for authentication
            domain: Windows domain (optional)

        Returns:
            True if connection successful or already connected, False otherwise
        """
        key = (server, share)

        with self._lock:
            # Check if already connected with same credentials
            if key in self._connections:
                existing = self._connections[key]
                if existing["username"] == username:
                    logger.info(f"[SMB-POOL] Reusing existing connection to {server}/{share}")
                    return True
                else:
                    # Different credentials - need to reconnect
                    logger.warning(f"[SMB-POOL] Credential change detected for {server}/{share}, reconnecting...")
                    self._disconnect_internal(server, share)

            # Check for conflicts with other shares on the same server
            conflict = self._find_server_conflict(server, username)
            if conflict:
                logger.error(
                    f"[SMB-POOL] Cannot connect to {server}/{share} - "
                    f"existing connection found: {conflict}. "
                    f"Windows only allows one credential set per server."
                )
                return False

            # Store credentials in Credential Manager first
            if username and password:
                if not self._store_credentials(server, username, password, domain):
                    logger.warning("[SMB-POOL] Failed to store credentials; trying explicit net use credentials")
                    return self._connect_with_explicit_credentials(server, share, username, password, domain)

            # Attempt connection
            unc_path = f"\\\\{server}\\{share}"
            cmd = ["net", "use", unc_path, "/persistent:no"]

            logger.debug(f"[SMB-POOL] Connecting to {unc_path}")
            result = subprocess.run(cmd, capture_output=True, text=True, creationflags=get_subprocess_flags())

            if result.returncode == 0:
                self._connections[key] = {
                    "username": username,
                    "connected_at": datetime.now(),
                    "server": server,
                    "share": share,
                }
                logger.info(f"[SMB-POOL] Successfully connected to {unc_path}")
                return True

            # Handle Error 1219 - connection already exists with different credentials
            if "1219" in result.stderr:
                existing_conn = self._find_existing_connection(server)
                if existing_conn:
                    logger.error(
                        f"[SMB-POOL] Error 1219: Cannot connect to {unc_path}.\n"
                        f"Existing connection: {existing_conn}\n"
                        f"Please disconnect the existing connection or use the same credentials."
                    )
                else:
                    logger.error("[SMB-POOL] Error 1219 detected but couldn't identify conflicting connection")
                return False

            # Handle Error 1223 - operation canceled (UAC/permission issue)
            if "1223" in result.stderr:
                logger.error(
                    f"[SMB-POOL] Error 1223: Operation canceled connecting to {unc_path}. "
                    f"This usually means:\n"
                    f"  1. Agent is not running with Administrator privileges\n"
                    f"  2. UAC is blocking the operation\n"
                    f"  3. Windows security policy is preventing credential storage\n"
                    f"Attempting connection with explicit credentials as fallback..."
                )
                # Try direct connection with explicit credentials
                return self._connect_with_explicit_credentials(server, share, username, password, domain)

            logger.error(f"[SMB-POOL] Connection failed: {result.stderr}")
            return False

    def connect_with_stdin(
        self,
        server: str,
        share: str,
        username: str,
        password: str,
        runner: Callable[..., subprocess.CompletedProcess[str]],
    ) -> bool:
        """Create a non-persistent connection without placing the password on argv.

        ``runner`` is deliberately injected so callers can enforce their own
        argv policy before the process starts.
        """
        unc_path = f"\\\\{server}\\{share}"
        result = runner(
            ["net", "use", unc_path, f"/user:{username}", "*", "/persistent:no"],
            input=f"{password}\n",
            capture_output=True,
            text=True,
            creationflags=get_subprocess_flags(),
        )
        return result.returncode == 0

    def connect_serverless(
        self,
        server: str,
        share: str,
        username: str,
        password: str,
        *,
        domain: str | None = None,
        is_system: bool = False,
    ) -> bool:
        """Connect without Credential Manager; SYSTEM may reclaim its own 1219 connection."""
        full_user = f"{domain}\\{username}" if domain else username
        unc_path = f"\\\\{server}\\{share}"

        def connect() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["net", "use", unc_path, f"/user:{full_user}", "*", "/persistent:no"],
                input=f"{password}\n",
                capture_output=True,
                text=True,
                creationflags=get_subprocess_flags(),
            )

        result = connect()
        if result.returncode == 0:
            return True
        if "1219" not in result.stderr:
            return False
        existing = self._find_existing_connection(server) or unc_path
        if not is_system:
            raise RuntimeError(f"SMB connection conflict: {existing}. Disconnect it or use the same credentials.")
        subprocess.run(
            ["net", "use", existing.split()[-1], "/delete", "/y"],
            capture_output=True,
            text=True,
            creationflags=get_subprocess_flags(),
        )
        return connect().returncode == 0

    def _find_server_conflict(self, server: str, username: str | None) -> str | None:
        """Check if there's a conflicting connection to this server.

        Returns the conflicting share name if found, None otherwise.
        """
        for (conn_server, conn_share), info in self._connections.items():
            if conn_server.lower() == server.lower() and info["username"] != username:
                return f"\\\\{conn_server}\\{conn_share} (user: {info['username']})"
        return None

    def _find_existing_connection(self, server: str) -> str | None:
        """Find existing net use connection to server.

        Returns the connection string if found, None otherwise.
        """
        try:
            result = subprocess.run(
                ["net", "use"], capture_output=True, text=True, creationflags=get_subprocess_flags()
            )

            for line in result.stdout.split("\n"):
                if server.lower() in line.lower() and "\\\\" in line:
                    return line.strip()
        except Exception as e:
            logger.debug(f"[SMB-POOL] Error checking existing connections: {e}")

        return None

    def _store_credentials(self, server: str, username: str, password: str, domain: str | None) -> bool:
        """Store credentials in Windows Credential Manager.

        Returns True if successful, False otherwise.
        """
        full_user = f"{domain}\\{username}" if domain else username

        # Delete any existing cached credentials for this server
        subprocess.run(
            ["cmdkey", "/delete", f"\\\\{server}"],
            capture_output=True,
            creationflags=get_subprocess_flags(),
        )

        # Add new credentials
        result = subprocess.run(
            ["cmdkey", "/add", f"\\\\{server}", "/user", full_user, "/pass", password],
            capture_output=True,
            text=True,
            creationflags=get_subprocess_flags(),
        )

        if result.returncode != 0:
            logger.debug(f"[SMB-POOL] cmdkey add failed: {result.stderr}")
            return False

        return True

    def _connect_with_explicit_credentials(
        self,
        server: str,
        share: str,
        username: str,
        password: str,
        domain: str | None = None,
    ) -> bool:
        """Fallback method to connect using explicit credentials in net use command.

        This bypasses cmdkey and passes credentials directly to net use.
        Used when Error 1223 occurs (UAC/permission issues preventing credential storage).

        Args:
            server: SMB server hostname or IP
            share: Share name
            username: Username for authentication
            password: Password for authentication
            domain: Optional domain name

        Returns:
            True if connection successful, False otherwise
        """
        unc_path = f"\\\\{server}\\{share}"
        full_user = f"{domain}\\{username}" if domain else username

        logger.info(f"[SMB-POOL] Attempting explicit credential connection to {unc_path}")

        # Try to connect with explicit credentials
        result = subprocess.run(
            ["net", "use", unc_path, f"/user:{full_user}", password],
            capture_output=True,
            text=True,
            creationflags=get_subprocess_flags(),
        )

        if result.returncode != 0:
            logger.error(f"[SMB-POOL] Explicit credential connection failed: {result.stderr}")
            return False

        # Track the connection
        key = (server, share)
        self._connections[key] = {
            "username": username,
            "domain": domain,
            "connected_at": datetime.now().isoformat(),
            "method": "explicit",  # Mark as using explicit credentials
        }

        logger.info(f"[SMB-POOL] Successfully connected to {unc_path} using explicit credentials")
        return True

    def disconnect(self, server: str, share: str) -> None:
        """Disconnect from a specific SMB share.

        Args:
            server: SMB server hostname or IP
            share: Share name
        """
        with self._lock:
            self._disconnect_internal(server, share)

    def _disconnect_internal(self, server: str, share: str) -> None:
        """Internal disconnect without lock (caller must hold lock)."""
        key = (server, share)
        unc_path = f"\\\\{server}\\{share}"

        # Remove the connection
        subprocess.run(
            ["net", "use", unc_path, "/delete", "/y"],
            capture_output=True,
            creationflags=get_subprocess_flags(),
        )

        # Clean up credentials if no other shares on this server
        has_other_shares = any(
            conn_server == server for (conn_server, conn_share) in self._connections.keys() if conn_share != share
        )

        if not has_other_shares:
            subprocess.run(
                ["cmdkey", "/delete", f"\\\\{server}"],
                capture_output=True,
                creationflags=get_subprocess_flags(),
            )

        # Remove from tracking
        self._connections.pop(key, None)
        logger.debug(f"[SMB-POOL] Disconnected from {unc_path}")

    def disconnect_all(self) -> None:
        """Disconnect all managed connections (cleanup on shutdown)."""
        with self._lock:
            for server, share in list(self._connections.keys()):
                self._disconnect_internal(server, share)
            logger.info("[SMB-POOL] All connections disconnected")

    def get_connection_status(self) -> dict[str, Any]:
        """Get current connection status for monitoring.

        Returns:
            Dictionary with connection information
        """
        with self._lock:
            return {
                "active_connections": len(self._connections),
                "connections": [
                    {
                        "server": conn["server"],
                        "share": conn["share"],
                        "username": conn["username"],
                        "connected_at": conn["connected_at"].isoformat(),
                    }
                    for conn in self._connections.values()
                ],
            }


def is_smb_path(path: str) -> bool:
    """Check if a path is an SMB/UNC path."""
    return path.startswith("//") or path.startswith("\\\\")


def is_nfs_path(path: str) -> bool:
    """Check if a path is an NFS path (server:/export format)."""
    # NFS paths look like: server:/export/path or 192.168.1.1:/share/path
    # But NOT like /local/path or C:\path
    if path.startswith(("/", "\\")) or "://" in path:
        return False
    if ":" in path:
        # Check it's not a Windows drive letter (C:)
        parts = path.split(":", 1)
        if len(parts) == 2 and len(parts[0]) > 1:
            # More than one char before colon, likely NFS
            return parts[1].startswith("/")
    return False


def parse_smb_path(path: str) -> tuple[str, str, str]:
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


def parse_nfs_path(path: str) -> tuple[str, str, str]:
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


def check_cifs_available() -> bool:
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


def check_nfs_available() -> bool:
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


@contextmanager
def smb_mount_context(
    server: str,
    share: str,
    username: str | None = None,
    password: str | None = None,
    domain: str | None = None,
    *,
    cifs_check: Callable[[], bool] = check_cifs_available,
) -> Generator[Path, None, None]:
    """Context manager that mounts an SMB share and yields the mount path.

    Used by Kopia on Linux, which needs a local filesystem path.
    Requires cifs-utils to be installed.
    """
    # Check if cifs-utils is available
    if not cifs_check():
        raise RuntimeError(
            "cifs-utils not installed. Install it with:\n"
            "  Debian/Ubuntu: sudo apt install cifs-utils\n"
            "  RHEL/Fedora: sudo dnf install cifs-utils\n"
            "  Arch: sudo pacman -S cifs-utils"
        )

    mount_point = Path(tempfile.mkdtemp(prefix="backer_smb_"))
    credentials_path: Path | None = None

    try:
        # Build mount command
        smb_url = f"//{server}/{share}"
        cmd = ["mount", "-t", "cifs", smb_url, str(mount_point)]

        # Build mount options
        opts = ["rw"]
        if username or password or domain:
            fd, credentials_name = tempfile.mkstemp(prefix="backer_smb_credentials_")
            os.close(fd)
            credentials_path = Path(credentials_name)
            credentials_path.write_text(
                "\n".join(
                    filter(
                        None,
                        (
                            f"username={username}" if username else "",
                            f"password={password}" if password else "",
                            f"domain={domain}" if domain else "",
                        ),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            credentials_path.chmod(0o600)
            opts.append(f"credentials={credentials_path}")
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
        if credentials_path:
            credentials_path.unlink(missing_ok=True)


@contextmanager
def nfs_mount_context(
    server: str,
    export_path: str,
    *,
    nfs_check: Callable[[], bool] = check_nfs_available,
) -> Generator[Path, None, None]:
    """Context manager that mounts an NFS export and yields the mount path.

    Used by Kopia on Linux, which needs a local filesystem path.
    Requires nfs-common (Debian/Ubuntu) or nfs-utils (RHEL/Fedora) to be installed.
    """
    # Check if NFS tools are available
    if not nfs_check():
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
