"""Storage repository management - SMB/NFS share discovery and browsing."""

import subprocess
import re
import os
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Generator


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


class RepositoryType(str, Enum):
    SMB = "smb"
    NFS = "nfs"
    LOCAL = "local"
    S3 = "s3"


@dataclass
class ShareInfo:
    """Information about a discovered share."""
    name: str
    share_type: str  # "Disk", "IPC", "Printer", etc.
    comment: str = ""


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
            # Build smbclient command
            cmd = ["smbclient", "-L", f"//{server}", "-g"]  # -g for parseable output

            if auth_path:
                cmd.extend(["-A", auth_path])
            else:
                cmd.append("-N")  # No password

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
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
            smb_path = f"/{path}/*"
        else:
            smb_path = "/*"

        with smb_auth_file(username, password, domain) as auth_path:
            # Build smbclient command
            cmd = ["smbclient", f"//{server}/{share}"]

            if auth_path:
                cmd.extend(["-A", auth_path])
            else:
                cmd.append("-N")

            cmd.extend(["-c", f"ls {smb_path}"])

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
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
                        entries.append(DirectoryEntry(
                            name=name,
                            is_dir=is_dir,
                            size=size,
                            modified=modified,
                        ))

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


class NFSBrowser:
    """Browse NFS exports on remote servers."""

    @staticmethod
    def list_exports(server: str) -> tuple[bool, list[ShareInfo] | str]:
        """List available NFS exports on a server.

        Returns:
            Tuple of (success, exports_list or error_message)
        """
        try:
            result = subprocess.run(
                ["showmount", "-e", server, "--no-headers"],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                error = result.stderr.strip()
                if "RPC" in error:
                    return False, "NFS server not responding - check if NFS is enabled"
                else:
                    return False, error or "Failed to query NFS exports"

            # Parse output: "/export/path  allowed_hosts"
            exports = []
            for line in result.stdout.split("\n"):
                line = line.strip()
                if line:
                    parts = line.split()
                    if parts:
                        export_path = parts[0]
                        allowed = " ".join(parts[1:]) if len(parts) > 1 else "*"
                        exports.append(ShareInfo(
                            name=export_path,
                            share_type="NFS",
                            comment=f"Allowed: {allowed}",
                        ))

            return True, exports

        except subprocess.TimeoutExpired:
            return False, "Connection timed out"
        except FileNotFoundError:
            return False, "showmount not installed - install nfs-common package"
        except Exception as e:
            return False, str(e)

    @staticmethod
    def list_directory(
        server: str,
        export: str,
        path: str = "",
    ) -> tuple[bool, list[DirectoryEntry] | str]:
        """List contents of a directory on an NFS export.

        Note: This requires temporarily mounting the export.
        """
        # Create temp mount point
        mount_point = Path(tempfile.mkdtemp(prefix="backer_nfs_"))

        try:
            # Mount the NFS export
            mount_cmd = ["mount", "-t", "nfs", f"{server}:{export}", str(mount_point)]
            result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=30)

            if result.returncode != 0:
                return False, f"Failed to mount: {result.stderr.strip()}"

            # List the directory
            full_path = mount_point / path.lstrip("/") if path else mount_point

            if not full_path.exists():
                return False, "Path not found"

            if not full_path.is_dir():
                return False, "Not a directory"

            entries = []
            for entry in full_path.iterdir():
                try:
                    stat = entry.stat()
                    entries.append(DirectoryEntry(
                        name=entry.name,
                        is_dir=entry.is_dir(),
                        size=stat.st_size,
                        modified=str(stat.st_mtime),
                    ))
                except (PermissionError, OSError):
                    continue

            entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
            return True, entries

        except subprocess.TimeoutExpired:
            return False, "Mount timed out"
        except PermissionError:
            return False, "Permission denied - may need root access for NFS"
        except Exception as e:
            return False, str(e)

        finally:
            # Always try to unmount and cleanup
            try:
                subprocess.run(["umount", str(mount_point)], capture_output=True, timeout=10)
            except Exception:
                pass
            try:
                mount_point.rmdir()
            except Exception:
                pass


class LocalBrowser:
    """Browse local filesystem directories."""

    @staticmethod
    def list_directory(path: str = "/") -> tuple[bool, list[DirectoryEntry] | str]:
        """List contents of a local directory."""
        try:
            dir_path = Path(path)

            if not dir_path.exists():
                return False, "Path not found"

            if not dir_path.is_dir():
                return False, "Not a directory"

            entries = []
            for entry in dir_path.iterdir():
                try:
                    stat = entry.stat()
                    entries.append(DirectoryEntry(
                        name=entry.name,
                        is_dir=entry.is_dir(),
                        size=stat.st_size if not entry.is_dir() else 0,
                        modified=str(stat.st_mtime),
                    ))
                except (PermissionError, OSError):
                    continue

            entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
            return True, entries

        except PermissionError:
            return False, "Permission denied"
        except Exception as e:
            return False, str(e)


def discover_shares(
    repo_type: RepositoryType,
    server: str,
    username: str | None = None,
    password: str | None = None,
    domain: str | None = None,
) -> tuple[bool, list[ShareInfo] | str]:
    """Discover available shares/exports on a server."""
    if repo_type == RepositoryType.SMB:
        return SMBBrowser.list_shares(server, username, password, domain)
    elif repo_type == RepositoryType.NFS:
        return NFSBrowser.list_exports(server)
    else:
        return False, f"Discovery not supported for {repo_type}"


def browse_directory(
    repo_type: RepositoryType,
    server: str,
    share: str,
    path: str = "",
    username: str | None = None,
    password: str | None = None,
    domain: str | None = None,
) -> tuple[bool, list[DirectoryEntry] | str]:
    """Browse a directory on a remote share."""
    if repo_type == RepositoryType.SMB:
        return SMBBrowser.list_directory(server, share, path, username, password, domain)
    elif repo_type == RepositoryType.NFS:
        return NFSBrowser.list_directory(server, share, path)
    elif repo_type == RepositoryType.LOCAL:
        full_path = f"{share}/{path}" if path else share
        return LocalBrowser.list_directory(full_path)
    else:
        return False, f"Browsing not supported for {repo_type}"
