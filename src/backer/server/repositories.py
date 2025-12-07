"""Storage repository management - SMB/NFS share discovery and browsing."""

import logging
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

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
            smb_path = f"/{path}/*"
        else:
            smb_path = "/*"

        with smb_auth_file(username, password, domain) as auth_path:
            # Build smbclient command
            cmd = ["smbclient", f"//{server}/{share}", "-t", "5"]  # 5 second connection timeout

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
                timeout=30,  # 30s for browsing
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

        Note: This requires temporarily mounting the export, which needs root/sudo.
        """
        # Create temp mount point
        mount_point = Path(tempfile.mkdtemp(prefix="backer_nfs_"))

        try:
            # Try mounting the NFS export
            # First try without sudo (works if running as root or setuid mount.nfs)
            mount_cmd = ["mount", "-t", "nfs", f"{server}:{export}", str(mount_point)]
            result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=30)

            # If that fails with permission error, try with sudo
            if result.returncode != 0:
                error_msg = result.stderr.strip().lower()
                if "permission" in error_msg or "setuid" in error_msg or "user" in error_msg:
                    # Try with sudo
                    mount_cmd = ["sudo", "-n", "mount", "-t", "nfs", f"{server}:{export}", str(mount_point)]
                    result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=30)

            if result.returncode != 0:
                error_msg = result.stderr.strip()
                # Provide helpful error for common NFS mount issues
                if "setuid" in error_msg.lower() or "user" in error_msg.lower():
                    return False, (
                        f"Failed to mount: {error_msg}\n\n"
                        "NFS mounts require root privileges. Options:\n"
                        "1. Run backer server as root\n"
                        "2. Add passwordless sudo for mount: echo 'backer ALL=(ALL) NOPASSWD: /usr/bin/mount, /usr/bin/umount' | sudo tee /etc/sudoers.d/backer-mount\n"
                        "3. Pre-mount the NFS share in /etc/fstab"
                    )
                return False, f"Failed to mount: {error_msg}"

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
            # Try without sudo first, then with sudo
            try:
                result = subprocess.run(["umount", str(mount_point)], capture_output=True, timeout=10)
                if result.returncode != 0:
                    subprocess.run(["sudo", "-n", "umount", str(mount_point)], capture_output=True, timeout=10)
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


def smb_read_file(
    server: str,
    share: str,
    remote_path: str,
    username: str | None = None,
    password: str | None = None,
    domain: str | None = None,
) -> tuple[bool, str]:
    """Read a file from an SMB share using smbclient.

    Args:
        server: SMB server hostname or IP
        share: Share name
        remote_path: Path to file within the share
        username: Optional username
        password: Optional password
        domain: Optional domain

    Returns:
        Tuple of (success, file_contents or error_message)
    """
    # Normalize path
    remote_path = remote_path.replace("\\", "/").lstrip("/")

    with smb_auth_file(username, password, domain) as auth_path:
        cmd = ["smbclient", f"//{server}/{share}", "-t", "5"]  # 5 second connection timeout

        if auth_path:
            cmd.extend(["-A", auth_path])
        else:
            cmd.append("-N")

        # Use 'get' command to download to stdout
        cmd.extend(["-c", f"get {remote_path} /dev/stdout"])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=30,  # 30s for reading files (connection timeout is 5s via -t flag)
            )

            if result.returncode != 0:
                error = result.stderr.decode("utf-8", errors="replace").strip()
                stdout_msg = result.stdout.decode("utf-8", errors="replace").strip()

                # Log full details for debugging
                logger.debug(
                    f"smbclient read failed: returncode={result.returncode}, "
                    f"stderr={error!r}, stdout={stdout_msg!r}"
                )

                if "NT_STATUS_OBJECT_NAME_NOT_FOUND" in error or "NT_STATUS_OBJECT_NAME_NOT_FOUND" in stdout_msg:
                    return False, "File not found"
                elif "NT_STATUS_ACCESS_DENIED" in error or "NT_STATUS_ACCESS_DENIED" in stdout_msg:
                    return False, "Access denied"
                elif "NT_STATUS_BAD_NETWORK_NAME" in error or "NT_STATUS_BAD_NETWORK_NAME" in stdout_msg:
                    return False, "Share not found (bad network name)"
                elif "NT_STATUS_OBJECT_PATH_NOT_FOUND" in error or "NT_STATUS_OBJECT_PATH_NOT_FOUND" in stdout_msg:
                    return False, "Path not found"
                elif "NT_STATUS_LOGON_FAILURE" in error or "NT_STATUS_LOGON_FAILURE" in stdout_msg:
                    return False, "Authentication failed"
                elif error:
                    return False, error
                elif stdout_msg:
                    # Sometimes errors go to stdout instead of stderr
                    return False, stdout_msg
                else:
                    return False, f"Command failed with exit code {result.returncode}"

            return True, result.stdout.decode("utf-8", errors="replace")

        except subprocess.TimeoutExpired:
            return False, "Connection timed out"
        except FileNotFoundError:
            return False, "smbclient not installed"
        except Exception as e:
            return False, str(e)


def smb_list_files(
    server: str,
    share: str,
    remote_path: str,
    username: str | None = None,
    password: str | None = None,
    domain: str | None = None,
) -> tuple[bool, list[str] | str]:
    """List files in a directory on an SMB share.

    Returns:
        Tuple of (success, list of filenames or error_message)
    """
    success, result = SMBBrowser.list_directory(
        server, share, remote_path, username, password, domain
    )

    if success:
        return True, [entry.name for entry in result]
    return False, result


def smb_file_exists(
    server: str,
    share: str,
    remote_path: str,
    username: str | None = None,
    password: str | None = None,
    domain: str | None = None,
) -> tuple[bool, bool]:
    """Check if a file/directory exists on an SMB share.

    Returns:
        Tuple of (exists, check_succeeded).
        If check_succeeded is False, exists value is unreliable.
    """
    # Get the parent directory and filename
    remote_path = remote_path.replace("\\", "/").strip("/")
    if "/" in remote_path:
        parent = "/".join(remote_path.split("/")[:-1])
        filename = remote_path.split("/")[-1]
    else:
        parent = ""
        filename = remote_path

    success, entries = smb_list_files(server, share, parent, username, password, domain)
    if success:
        return filename in entries, True
    # Listing failed - we can't determine if file exists
    return False, False


def smb_delete_file(
    server: str,
    share: str,
    remote_path: str,
    username: str | None = None,
    password: str | None = None,
    domain: str | None = None,
) -> tuple[bool, str]:
    """Delete a single file from an SMB share.

    Returns:
        Tuple of (success, message)
    """
    remote_path = remote_path.replace("\\", "/").strip("/")

    with smb_auth_file(username, password, domain) as auth_path:
        cmd = ["smbclient", f"//{server}/{share}", "-t", "5"]  # 5 second connection timeout

        if auth_path:
            cmd.extend(["-A", auth_path])
        else:
            cmd.append("-N")

        cmd.extend(["-c", f'del "{remote_path}"'])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)  # 10 second total timeout

            if result.returncode == 0:
                return True, "File deleted"
            elif "NT_STATUS_NO_SUCH_FILE" in result.stderr or "NT_STATUS_OBJECT_NAME_NOT_FOUND" in result.stderr:
                return True, "File already deleted"
            else:
                return False, result.stderr.strip() or "Delete failed"

        except subprocess.TimeoutExpired:
            return False, "Command timed out"
        except Exception as e:
            return False, str(e)


def smb_delete_directory(
    server: str,
    share: str,
    remote_path: str,
    username: str | None = None,
    password: str | None = None,
    domain: str | None = None,
) -> tuple[bool, str]:
    """Recursively delete a directory on an SMB share.

    Args:
        server: SMB server hostname/IP
        share: Share name
        remote_path: Path to directory within share
        username: Optional username
        password: Optional password
        domain: Optional domain

    Returns:
        Tuple of (success, message)
    """
    import logging

    logger = logging.getLogger(__name__)
    remote_path = remote_path.replace("\\", "/").strip("/")

    def run_smb_command(commands: str) -> tuple[int, str, str]:
        """Run smbclient with given commands."""
        with smb_auth_file(username, password, domain) as auth_path:
            cmd = ["smbclient", f"//{server}/{share}", "-t", "5"]  # 5 second connection timeout

            if auth_path:
                cmd.extend(["-A", auth_path])
            else:
                cmd.append("-N")

            cmd.extend(["-c", commands])

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)  # 10 second total
                return result.returncode, result.stdout, result.stderr
            except subprocess.TimeoutExpired:
                return -1, "", "Command timed out"
            except Exception as e:
                return -1, "", str(e)

    try:
        # First, check if directory exists
        exists, check_ok = smb_file_exists(server, share, remote_path, username, password, domain)
        if check_ok and not exists:
            return True, "Directory does not exist (already deleted)"

        # For backer job metadata, we know the structure:
        # {job_name}/config.json
        # {job_name}/runs/*.json
        # So we can delete in the right order

        deleted_files = []
        errors = []

        # Delete files in runs/ subdirectory
        runs_path = f"{remote_path}/runs"
        success, files = smb_list_files(server, share, runs_path, username, password, domain)
        if success and files:
            for f in files:
                rc, stdout, err = run_smb_command(f'del "{runs_path}/{f}"')
                if rc == 0 or "NT_STATUS_NO_SUCH_FILE" in err or "NT_STATUS_OBJECT_NAME_NOT_FOUND" in err:
                    deleted_files.append(f"{runs_path}/{f}")
                else:
                    errors.append(f"del {runs_path}/{f}: {err.strip()}")
                    logger.warning(f"SMB delete failed: del {runs_path}/{f}: {err.strip()}")

        # Delete runs directory
        rc, stdout, err = run_smb_command(f'rmdir "{runs_path}"')
        if rc == 0 or "NT_STATUS_NO_SUCH_FILE" in err or "NT_STATUS_OBJECT_NAME_NOT_FOUND" in err:
            deleted_files.append(runs_path)
        elif "NT_STATUS_DIRECTORY_NOT_EMPTY" in err:
            errors.append(f"rmdir {runs_path}: directory not empty")
            logger.warning("SMB delete: runs dir not empty")
        else:
            errors.append(f"rmdir {runs_path}: {err.strip()}")

        # Delete config.json
        rc, stdout, err = run_smb_command(f'del "{remote_path}/config.json"')
        if rc == 0 or "NT_STATUS_NO_SUCH_FILE" in err or "NT_STATUS_OBJECT_NAME_NOT_FOUND" in err:
            deleted_files.append(f"{remote_path}/config.json")
        else:
            errors.append(f"del config.json: {err.strip()}")
            logger.warning(f"SMB delete failed: del config.json: {err.strip()}")

        # Delete the job directory itself
        rc, stdout, err = run_smb_command(f'rmdir "{remote_path}"')
        if rc == 0 or "NT_STATUS_NO_SUCH_FILE" in err or "NT_STATUS_OBJECT_NAME_NOT_FOUND" in err:
            deleted_files.append(remote_path)
        elif "NT_STATUS_DIRECTORY_NOT_EMPTY" in err:
            errors.append(f"rmdir {remote_path}: directory not empty")
            logger.warning("SMB delete: job dir not empty - may have extra files")
        else:
            errors.append(f"rmdir {remote_path}: {err.strip()}")
            logger.warning(f"SMB delete failed: rmdir {remote_path}: {err.strip()}")

        # Verify deletion
        exists, check_ok = smb_file_exists(server, share, remote_path, username, password, domain)
        if check_ok and not exists:
            return True, f"Deleted {len(deleted_files)} items"
        elif not check_ok:
            # Can't verify - assume success if we deleted the main files
            if f"{remote_path}/config.json" in deleted_files:
                return True, f"Deleted {len(deleted_files)} items (verification unavailable)"
            else:
                return False, "Could not verify deletion and config.json not confirmed deleted"
        elif errors:
            return False, f"Deletion incomplete: {'; '.join(errors[:3])}"
        else:
            return False, "Directory still exists after deletion attempt"

    except subprocess.TimeoutExpired:
        return False, "Command timed out"
    except Exception as e:
        logger.exception(f"Error in smb_delete_directory: {e}")
        return False, str(e)
