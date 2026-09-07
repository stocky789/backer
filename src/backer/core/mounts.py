"""SMB/NFS path parsing and mount helpers."""

import logging
import os
import re
import shutil
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
        self.serverless_session_created = False

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
                        f"Existing connection: {existing_conn[0]}\n"
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
        self.serverless_session_created = False
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
            self.serverless_session_created = True
            return True
        if "1219" not in result.stderr:
            return False
        existing = self._find_existing_connection(server)
        if not existing:
            return False
        existing_path, existing_user = existing
        if existing_user and existing_user.casefold() == full_user.casefold():
            return True
        if not is_system:
            raise RuntimeError(f"SMB connection conflict: {existing_path}. Disconnect it or use the same credentials.")
        if not existing_user:
            return False
        removed = subprocess.run(
            ["net", "use", existing_path, "/delete", "/y"],
            capture_output=True,
            text=True,
            creationflags=get_subprocess_flags(),
        )
        if removed.returncode:
            return False
        result = connect()
        self.serverless_session_created = result.returncode == 0
        return self.serverless_session_created

    def disconnect_serverless(self, server: str, share: str) -> None:
        """Remove only the non-persistent session this serverless invocation created."""
        subprocess.run(
            ["net", "use", f"\\\\{server}\\{share}", "/delete", "/y"],
            capture_output=True,
            text=True,
            creationflags=get_subprocess_flags(),
        )

    def connect_existing_serverless(self, server: str, share: str, path: str = "") -> bool:
        """Reuse Windows' named SMB session and prove the selected folder is writable."""
        if not self._find_existing_connection(server):
            return False
        unc_path = f"\\\\{server}\\{share}"
        result = subprocess.run(
            ["net", "use", unc_path, "/persistent:no"],
            capture_output=True,
            text=True,
            creationflags=get_subprocess_flags(),
        )
        if result.returncode:
            return False
        target = Path(unc_path, *[part for part in path.replace("\\", "/").split("/") if part])
        probe = target / f".backer-write-probe-{os.urandom(8).hex()}"
        try:
            probe.write_bytes(b"")
            probe.unlink()
            return True
        except OSError:
            probe.unlink(missing_ok=True)
            return False

    def disconnect_existing_connection(self, connection: str) -> bool:
        """Disconnect one explicitly named Windows connection after user confirmation."""
        if not connection.startswith("\\\\") or any(part in connection for part in ("*", "?")):
            return False
        result = subprocess.run(
            ["net", "use", connection, "/delete", "/y"],
            capture_output=True,
            text=True,
            creationflags=get_subprocess_flags(),
        )
        return result.returncode == 0

    def _find_server_conflict(self, server: str, username: str | None) -> str | None:
        """Check if there's a conflicting connection to this server.

        Returns the conflicting share name if found, None otherwise.
        """
        for (conn_server, conn_share), info in self._connections.items():
            if conn_server.lower() == server.lower() and info["username"] != username:
                return f"\\\\{conn_server}\\{conn_share} (user: {info['username']})"
        return None

    def _find_existing_connection(self, server: str) -> tuple[str, str | None] | None:
        """Find existing net use connection to server.

        Returns the connection string if found, None otherwise.
        """
        try:
            result = subprocess.run(
                ["net", "use"], capture_output=True, text=True, creationflags=get_subprocess_flags()
            )

            for line in result.stdout.split("\n"):
                if server.lower() in line.lower() and "\\\\" in line:
                    connection = line.strip().split()[-1]
                    details = subprocess.run(
                        ["net", "use", connection], capture_output=True, text=True, creationflags=get_subprocess_flags()
                    )
                    username = None
                    for detail in details.stdout.splitlines():
                        if "user name" in detail.lower() and ":" in detail:
                            username = detail.split(":", 1)[1].strip() or None
                            break
                    return connection, username
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


def sudo_available() -> bool:
    """Whether the current user can run the mount commands without a prompt."""
    try:
        return subprocess.run(["sudo", "-n", "true"], capture_output=True, text=True, timeout=5).returncode == 0
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


def _unescape_mount_field(field: str) -> str:
    """/proc/mounts escapes space, tab, newline and backslash as octal."""
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), field)


def find_existing_cifs_mount(server: str, share: str, proc_mounts: str = "/proc/mounts") -> Path | None:
    """Return the mount point of an existing kernel cifs mount of //server/share."""
    wanted = f"//{server}/{share}".casefold()
    try:
        lines = Path(proc_mounts).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        parts = line.split()
        if len(parts) < 3 or parts[2] not in ("cifs", "smb3"):
            continue
        device = _unescape_mount_field(parts[0]).replace("\\", "/").casefold()
        if device.rstrip("/") == wanted.rstrip("/"):
            return Path(_unescape_mount_field(parts[1]))
    return None


def gvfs_dir() -> Path:
    """Where gvfsd-fuse exposes user mounts."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "gvfs"


def gvfs_available() -> bool:
    """gio plus a session bus is all gvfsd needs; no TTY and no desktop session."""
    if not shutil.which("gio"):
        return False
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return True
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return bool(runtime) and Path(runtime, "bus").exists()


def find_gvfs_mount(server: str, share: str) -> Path | None:
    """Find the gvfs FUSE entry for //server/share, matching fields case-insensitively."""
    root = gvfs_dir()
    try:
        entries = list(root.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if not entry.name.startswith("smb-share:"):
            continue
        fields = dict(
            part.split("=", 1) for part in entry.name[len("smb-share:") :].split(",") if "=" in part
        )
        if (
            fields.get("server", "").casefold() == server.casefold()
            and fields.get("share", "").casefold() == share.casefold()
        ):
            return entry
    # gvfs normalises the share to lower case; fall back to the canonical name in
    # case the FUSE directory refuses to be listed.
    canonical = root / f"smb-share:server={server.lower()},share={share.lower()}"
    return canonical if canonical.exists() else None


def gvfs_smb_error(text: str) -> str:
    """Map `gio mount` failure text onto the wording the other SMB paths use."""
    lowered = text.lower()
    if any(token in lowered for token in ("connection refused", "host is down", "unreachable", "timed out")):
        return "Host unreachable - check network connection"
    if "no such file or directory" in lowered or "not found" in lowered or "does not exist" in lowered:
        return "Server not found or not accessible"
    if "authentication required" in lowered or "permission denied" in lowered or "access denied" in lowered:
        return "Login failed - invalid username or password"
    return text.strip() or "Unknown error mounting the share"


def gvfs_mount(
    server: str,
    share: str,
    username: str | None,
    password: str | None,
    domain: str | None,
) -> Path:
    """Mount //server/share through gvfs as the current user and return the FUSE path.

    The mount is session-scoped and is deliberately left mounted, exactly as a file
    manager leaves it. Credentials go on stdin: gio prompts for user, domain and
    password in that order, and an empty domain line accepts its default.
    """
    existing = find_gvfs_mount(server, share)
    if existing:
        return existing

    answers = f"{username or ''}\n{domain or ''}\n{password or ''}\n"
    try:
        result = subprocess.run(
            ["gio", "mount", f"smb://{server}/{share}"],
            input=answers,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = f"{result.stdout}\n{result.stderr}"
    except subprocess.TimeoutExpired:
        output = "timed out"

    # gio exits 0 after a failed authentication in some versions, so the entry
    # appearing is the only trustworthy proof that the share is mounted.
    mounted = find_gvfs_mount(server, share)
    if mounted:
        return mounted
    if password:
        output = output.replace(password, "***")
    raise RuntimeError(f"Failed to mount //{server}/{share}: {gvfs_smb_error(output)}")


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
    """Context manager that yields a local filesystem path for //server/share.

    Kopia on Linux needs a path, and there are four ways to get one, tried in order:
    an existing kernel mount, a passwordless-sudo or root `mount -t cifs`, an
    unprivileged gvfs mount, or a refusal that explains how to get one of the first three.
    """
    # 1. Someone already mounted it - use it, and never unmount what is not ours.
    existing = find_existing_cifs_mount(server, share)
    if existing:
        print(f"[SMB] Reusing existing mount of //{server}/{share} at {existing}")
        yield existing
        return

    is_root = not hasattr(os, "geteuid") or os.geteuid() == 0
    cifs_available = cifs_check()
    use_sudo = not is_root and cifs_available and sudo_available()

    # Prefer the kernel client whenever it can run without prompting. Unlike gvfs,
    # it gives Kopia normal filesystem semantics for its pack-file writes.
    if not is_root and not use_sudo and gvfs_available():
        mount_point = gvfs_mount(server, share, username, password, domain)
        print(f"[SMB] Mounted //{server}/{share} through gvfs at {mount_point}")
        yield mount_point
        # Deliberately left mounted: it is session-scoped and shared with the
        # file manager, so unmounting would pull it out from under the user.
        return

    if not cifs_available:
        raise RuntimeError(
            "cifs-utils not installed. Install it with:\n"
            "  Debian/Ubuntu: sudo apt install cifs-utils\n"
            "  RHEL/Fedora: sudo dnf install cifs-utils\n"
            "  Arch: sudo pacman -S cifs-utils"
        )

    # 4. mount -t cifs needs elevation; without it mount.cifs fails with a misleading
    # fstab message, so refuse up front with the supported alternatives.
    if not is_root and not use_sudo:
        raise RuntimeError(
            "Mounting an SMB share needs root privileges on Linux. Install gvfs to "
            "mount it as your own user with no password prompt (Debian/Ubuntu: "
            "sudo apt install gvfs gvfs-backends; RHEL/Fedora: sudo dnf install gvfs "
            "gvfs-smb; Arch: sudo pacman -S gvfs gvfs-smb). Otherwise mount the share "
            "yourself first (through your file manager or /etc/fstab) and add the "
            "mounted folder as a local repository, or run this command as root."
        )

    # 2. The kernel cifs mount, owned by us and unmounted on the way out.
    mount_point = Path(tempfile.mkdtemp(prefix="backer_smb_"))
    credentials_path: Path | None = None
    mounted = False
    operation_error: BaseException | None = None

    try:
        # Build mount command
        smb_url = f"//{server}/{share}"
        privilege = ["sudo", "-n"] if use_sudo else []
        cmd = [*privilege, "mount", "-t", "cifs", smb_url, str(mount_point)]

        # Build mount options
        opts = ["rw", f"uid={os.getuid()}", f"gid={os.getgid()}"]
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
            if password:
                error_msg = error_msg.replace(password, "***")
            if "Permission denied" in error_msg:
                raise RuntimeError(f"SMB mount permission denied. Check credentials for //{server}/{share}")
            elif "No such file or directory" in error_msg:
                raise RuntimeError(f"SMB share not found: //{server}/{share}")
            elif "Connection refused" in error_msg or "Host is down" in error_msg:
                raise RuntimeError(f"Cannot connect to SMB server: {server}")
            else:
                raise RuntimeError(f"Failed to mount SMB share: {error_msg}")

        mounted = True
        print("[SMB] Mounted successfully")
        try:
            yield mount_point
        except BaseException as error:
            operation_error = error
            raise

    finally:
        cleanup_error: BaseException | None = None
        if mounted:
            print(f"[SMB] Unmounting {mount_point}")
            try:
                result = subprocess.run(
                    [*privilege, "umount", str(mount_point)], capture_output=True, text=True, timeout=30
                )
                if result.returncode != 0:
                    cleanup_error = RuntimeError(f"Failed to unmount SMB share: {result.stderr.strip()}")
            except Exception as error:
                cleanup_error = RuntimeError(f"Failed to unmount SMB share: {error}")
        if not mounted or cleanup_error is None:
            try:
                mount_point.rmdir()
            except OSError as error:
                cleanup_error = cleanup_error or error
        if credentials_path:
            credentials_path.unlink(missing_ok=True)
        if cleanup_error:
            if operation_error:
                raise cleanup_error from operation_error
            raise cleanup_error


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
