"""SMB share discovery and browsing."""

import logging
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@contextmanager
def smb_auth_file(
    username: str | None,
    password: str | None,
    domain: str | None = None,
) -> Generator[str | None, None, None]:
    """Create a temporary SMB authentication file.

    This is more secure than passing passwords on the command line,
    as command line arguments are visible in process listings.

    The auth file format is:
        username = <user>
        password = <pass>
        domain = <domain>

    Yields:
        Path to the auth file, or None if no credentials provided
    """
    if not username or not password:
        yield None
        return

    # Create a secure temp file
    fd, auth_path = tempfile.mkstemp(prefix="smb_auth_", suffix=".txt")

    try:
        # Write credentials to file
        auth_content = f"username = {username}\npassword = {password}\n"
        if domain:
            auth_content += f"domain = {domain}\n"

        os.write(fd, auth_content.encode("utf-8"))
        os.close(fd)

        # Set restrictive permissions (owner read only)
        if sys.platform != "win32":
            os.chmod(auth_path, 0o400)

        yield auth_path

    finally:
        # Securely delete the file
        try:
            # Overwrite with zeros before deleting
            with open(auth_path, "wb") as f:
                f.write(b"\x00" * 256)
            os.unlink(auth_path)
        except Exception:
            pass


@dataclass
class ShareInfo:
    """Information about a discovered share."""

    name: str
    share_type: str  # "Disk", "IPC", "Printer", etc.
    comment: str = ""
    path: str = ""  # Full path for local directories


@dataclass
class DirectoryEntry:
    """A file or directory entry."""

    name: str
    is_dir: bool
    size: int = 0
    modified: str = ""


class SMBBrowser:
    """Browse SMB/CIFS shares on remote servers."""

    @staticmethod
    def list_shares(
        server: str,
        username: str | None = None,
        password: str | None = None,
        domain: str | None = None,
    ) -> tuple[bool, list[ShareInfo] | str]:
        """List available shares on an SMB server.

        Returns:
            Tuple of (success, shares_list or error_message)
        """
        with smb_auth_file(username, password, domain) as auth_path:
            # Build smbclient command with connection timeout
            cmd = ["smbclient", "-L", f"//{server}", "-g", "-t", "5"]  # -g for parseable output, 5s connection timeout

            if auth_path:
                cmd.extend(["-A", auth_path])
            else:
                cmd.append("-N")  # No password

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,  # 30s for browsing (connection timeout is 5s via -t flag)
                )

                if result.returncode != 0:
                    error = result.stderr.strip()
                    if "NT_STATUS_ACCESS_DENIED" in error:
                        return False, "Access denied - check credentials"
                    elif "NT_STATUS_BAD_NETWORK_NAME" in error:
                        return False, "Server not found or not accessible"
                    elif "NT_STATUS_LOGON_FAILURE" in error:
                        return False, "Login failed - invalid username or password"
                    elif "NT_STATUS_HOST_UNREACHABLE" in error:
                        return False, "Host unreachable - check network connection"
                    else:
                        return False, error or "Unknown error connecting to server"

                # Parse output: format is "type|name|comment"
                shares = []
                for line in result.stdout.split("\n"):
                    line = line.strip()
                    if line.startswith("Disk|") or line.startswith("IPC|") or line.startswith("Printer|"):
                        parts = line.split("|")
                        if len(parts) >= 2:
                            share_type = parts[0]
                            name = parts[1]
                            comment = parts[2] if len(parts) > 2 else ""
                            # Skip IPC$ and other system shares
                            if not name.endswith("$") and share_type == "Disk":
                                shares.append(ShareInfo(name=name, share_type=share_type, comment=comment))

                return True, shares

            except subprocess.TimeoutExpired:
                return False, "Connection timed out"
            except FileNotFoundError:
                return False, "smbclient not installed - install samba-client package"
            except Exception as e:
                return False, str(e)

    @staticmethod
    def list_directory(
        server: str,
        share: str,
        path: str = "",
        username: str | None = None,
        password: str | None = None,
        domain: str | None = None,
    ) -> tuple[bool, list[DirectoryEntry] | str]:
        """List contents of a directory on an SMB share.

        Args:
            server: SMB server hostname or IP
            share: Share name
            path: Path within the share (empty for root)
            username: Optional username
            password: Optional password
            domain: Optional domain

        Returns:
            Tuple of (success, entries_list or error_message)
        """
        # Normalize path
        if path:
            path = path.replace("\\", "/").strip("/")
            # For paths with spaces, we need to cd into the directory first, then ls
            # Direct ls with quoted paths containing wildcards doesn't work well in smbclient
            smb_path = f"/{path}"
        else:
            smb_path = ""

        with smb_auth_file(username, password, domain) as auth_path:
            # Build smbclient command
            cmd = ["smbclient", f"//{server}/{share}", "-t", "5"]  # 5 second connection timeout

            if auth_path:
                cmd.extend(["-A", auth_path])
            else:
                cmd.append("-N")

            # Use cd + ls to handle paths with spaces (like "Virtual Machines")
            # Quoting the path in cd works better than trying to quote ls with wildcards
            if smb_path:
                cmd.extend(["-c", f'cd "{smb_path}"; ls'])
            else:
                cmd.extend(["-c", "ls"])

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,  # 30s for browsing (connection timeout is 5s via -t flag)
                )

                if result.returncode != 0:
                    error = result.stderr.strip()
                    if "NT_STATUS_ACCESS_DENIED" in error:
                        return False, "Access denied to this directory"
                    elif "NT_STATUS_OBJECT_NAME_NOT_FOUND" in error:
                        return False, "Directory not found"
                    else:
                        return False, error or "Failed to list directory"

                # Parse directory listing
                entries = []
                for line in result.stdout.split("\n"):
                    line = line.strip()
                    # Format: "  filename                          D        0  Wed Dec  3 10:15:30 2025"
                    # Or:     "  filename                                1234  Wed Dec  3 10:15:30 2025"
                    match = re.match(r"^\s*(.+?)\s+([DAHN]*)\s+(\d+)\s+(.+)$", line)
                    if match:
                        name = match.group(1).strip()
                        attrs = match.group(2)
                        size = int(match.group(3))
                        modified = match.group(4)

                        # Skip . and ..
                        if name in (".", ".."):
                            continue

                        is_dir = "D" in attrs
                        entries.append(
                            DirectoryEntry(
                                name=name,
                                is_dir=is_dir,
                                size=size,
                                modified=modified,
                            )
                        )

                # Sort: directories first, then files
                entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
                return True, entries

            except subprocess.TimeoutExpired:
                return False, "Connection timed out"
            except FileNotFoundError:
                return False, "smbclient not installed"
            except Exception as e:
                return False, str(e)

    @staticmethod
    def make_directory(
        server: str,
        share: str,
        path: str,
        username: str | None = None,
        password: str | None = None,
        domain: str | None = None,
    ) -> bool:
        """Create one relative folder without putting credentials on argv."""
        path = path.replace("\\", "/").strip("/")
        if not path or '"' in path or any(part in {"", ".", ".."} for part in path.split("/")):
            return False
        with smb_auth_file(username, password, domain) as auth_path:
            command = ["smbclient", f"//{server}/{share}", "-t", "5"]
            command.extend(["-A", auth_path] if auth_path else ["-N"])
            command.extend(["-c", f'mkdir "{path}"'])
            try:
                return subprocess.run(command, capture_output=True, text=True, timeout=30).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                return False

    @staticmethod
    def test_connection(
        server: str,
        share: str,
        username: str | None = None,
        password: str | None = None,
        domain: str | None = None,
    ) -> tuple[bool, str]:
        """Test if we can connect to the share.

        Returns:
            Tuple of (success, message)
        """
        success, result = SMBBrowser.list_directory(
            server=server,
            share=share,
            path="",
            username=username,
            password=password,
            domain=domain,
        )

        if success:
            return True, f"Successfully connected to //{server}/{share}"
        else:
            return False, result
