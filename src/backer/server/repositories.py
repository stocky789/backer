"""Storage repository management - SMB/NFS share discovery and browsing."""

import logging
import os
import subprocess
import tempfile
from enum import Enum
from pathlib import Path

from backer.core.smb_browse import DirectoryEntry, ShareInfo, SMBBrowser, smb_auth_file, smbclient_command

logger = logging.getLogger(__name__)


class RepositoryType(str, Enum):
    SMB = "smb"
    NFS = "nfs"
    LOCAL = "local"
    S3 = "s3"


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
                        exports.append(
                            ShareInfo(
                                name=export_path,
                                share_type="NFS",
                                comment=f"Allowed: {allowed}",
                            )
                        )

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
            # Use explicit options to avoid inheriting fstab defaults
            # -o nfsvers=3 provides broad compatibility, soft,timeo=50 prevents hangs
            nfs_opts = "soft,timeo=50,retrans=2"

            # First try without sudo (works if running as root or setuid mount.nfs)
            mount_cmd = ["mount", "-t", "nfs", "-o", nfs_opts, f"{server}:{export}", str(mount_point)]
            result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=30)

            # If that fails with permission error, try with sudo
            if result.returncode != 0:
                error_msg = result.stderr.strip().lower()
                perm_errors = ("permission", "setuid", "user", "fstab")
                if any(err in error_msg for err in perm_errors):
                    # Try with sudo
                    mount_cmd = [
                        "sudo",
                        "-n",
                        "mount",
                        "-t",
                        "nfs",
                        "-o",
                        nfs_opts,
                        f"{server}:{export}",
                        str(mount_point),
                    ]
                    result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=30)

            if result.returncode != 0:
                error_msg = result.stderr.strip()
                # Provide helpful error for common NFS mount issues
                perm_keywords = ("setuid", "user", "fstab")
                if any(kw in error_msg.lower() for kw in perm_keywords):
                    return False, (
                        f"Failed to mount: {error_msg}\n\n"
                        "NFS mounts require root privileges. Options:\n"
                        "1. Run backer server as root\n"
                        "2. Add passwordless sudo for mount:\n"
                        "   echo 'backer ALL=(ALL) NOPASSWD: /usr/bin/mount, /usr/bin/umount' "
                        "| sudo tee /etc/sudoers.d/backer-mount\n"
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
                    entries.append(
                        DirectoryEntry(
                            name=entry.name,
                            is_dir=entry.is_dir(),
                            size=stat.st_size,
                            modified=str(stat.st_mtime),
                        )
                    )
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
    def test_connection(path: str) -> tuple[bool, str]:
        """Test if a local directory is accessible."""
        import getpass
        import stat

        try:
            dir_path = Path(path)
            current_user = getpass.getuser()
            current_uid = os.getuid() if hasattr(os, "getuid") else "N/A"

            logger.info(f"Testing local path: {path}")
            logger.info(f"Current user: {current_user} (UID: {current_uid})")

            if not dir_path.exists():
                msg = f"Path does not exist: {path}"
                logger.warning(msg)
                return False, msg

            if not dir_path.is_dir():
                msg = f"Not a directory: {path}"
                logger.warning(msg)
                return False, msg

            # Get directory stats for debugging
            try:
                st = dir_path.stat()
                dir_mode = stat.filemode(st.st_mode)
                dir_owner = st.st_uid
                logger.info(f"Directory permissions: {dir_mode} owner UID: {dir_owner}")
            except Exception as e:
                logger.warning(f"Could not stat directory: {e}")

            # Check read/write permissions
            readable = os.access(dir_path, os.R_OK)
            writable = os.access(dir_path, os.W_OK)
            logger.info(f"Permission checks - readable: {readable}, writable: {writable}")

            if not readable:
                msg = f"Path is not readable: {path}"
                logger.warning(msg)
                return False, msg

            if not writable:
                msg = f"Path is not writable: {path}"
                logger.warning(msg)
                return False, msg

            # Try to list directory contents as additional verification
            try:
                entries = list(dir_path.iterdir())
                logger.info(f"Successfully listed {len(entries)} entries in {path}")
            except PermissionError as e:
                msg = f"Cannot list directory contents: {path} - {e}"
                logger.error(msg)
                return False, msg

            msg = f"Local directory accessible: {path}"
            logger.info(msg)
            return True, msg

        except PermissionError as e:
            msg = f"Permission denied: {e}"
            logger.error(msg)
            return False, msg
        except Exception as e:
            msg = f"Error accessing path: {e}"
            logger.error(msg, exc_info=True)
            return False, msg

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
                    entries.append(
                        DirectoryEntry(
                            name=entry.name,
                            is_dir=entry.is_dir(),
                            size=stat.st_size if not entry.is_dir() else 0,
                            modified=str(stat.st_mtime),
                        )
                    )
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
    elif repo_type == RepositoryType.LOCAL:
        # For LOCAL repos, return root directories as "shares"
        try:
            root = Path("/")
            shares = []
            for entry in root.iterdir():
                if entry.is_dir():
                    try:
                        # Only include accessible directories
                        list(entry.iterdir())
                        shares.append(
                            ShareInfo(
                                name=entry.name,
                                path=str(entry),
                                share_type="directory",
                            )
                        )
                    except (PermissionError, OSError):
                        continue
            shares.sort(key=lambda s: s.name.lower())
            return True, shares
        except Exception as e:
            return False, f"Error listing root directories: {e}"
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
        cmd = smbclient_command(f"//{server}/{share}", "-t", "5")  # 5 second connection timeout

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
                    f"smbclient read failed: returncode={result.returncode}, stderr={error!r}, stdout={stdout_msg!r}"
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
    success, result = SMBBrowser.list_directory(server, share, remote_path, username, password, domain)

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


def smb_write_file(
    server: str,
    share: str,
    remote_path: str,
    content: str | bytes,
    username: str | None = None,
    password: str | None = None,
    domain: str | None = None,
) -> tuple[bool, str]:
    """Write content to a file on an SMB share using smbclient.

    Args:
        server: SMB server hostname or IP
        share: Share name
        remote_path: Path to file within the share
        content: File content (string or bytes)
        username: Optional username
        password: Optional password
        domain: Optional domain

    Returns:
        Tuple of (success, message or error)
    """
    # Normalize path
    remote_path = remote_path.replace("\\", "/").strip("/")

    # Create temp file with content
    fd, temp_path = tempfile.mkstemp(prefix="smb_write_", suffix=".tmp")

    try:
        # Write content to temp file
        if isinstance(content, str):
            os.write(fd, content.encode("utf-8"))
        else:
            os.write(fd, content)
        os.close(fd)

        # Upload to SMB share
        with smb_auth_file(username, password, domain) as auth_path:
            cmd = smbclient_command(f"//{server}/{share}", "-t", "5")

            if auth_path:
                cmd.extend(["-A", auth_path])
            else:
                cmd.append("-N")

            # Use 'put' command to upload file
            cmd.extend(["-c", f'put "{temp_path}" "{remote_path}"'])

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
                        return False, "Access denied"
                    elif "NT_STATUS_OBJECT_PATH_NOT_FOUND" in error:
                        return False, "Parent directory not found"
                    else:
                        return False, error or "Upload failed"

                return True, "File written successfully"

            except subprocess.TimeoutExpired:
                return False, "Connection timed out"
            except FileNotFoundError:
                return False, "smbclient not installed"
            except Exception as e:
                return False, str(e)

    finally:
        # Clean up temp file
        try:
            os.unlink(temp_path)
        except Exception:
            pass


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
        cmd = smbclient_command(f"//{server}/{share}", "-t", "5")  # 5 second connection timeout

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


def nfs_delete_directory(
    server: str,
    export: str,
    remote_path: str,
) -> tuple[bool, str]:
    """Recursively delete a directory on an NFS export.

    Temporarily mounts the NFS export, deletes the directory, then unmounts.

    Args:
        server: NFS server hostname/IP
        export: NFS export path (e.g., /mnt/tank/backups)
        remote_path: Path to directory within export to delete (MUST be non-empty)

    Returns:
        Tuple of (success, message)
    """
    import shutil

    # SAFETY: Refuse to delete export root - remote_path must be specified
    remote_path = remote_path.strip("/")
    if not remote_path:
        logger.error("[NFS DELETE] Refusing to delete export root - remote_path is empty")
        return False, "Cannot delete export root - path must be specified"

    logger.info(f"[NFS DELETE] Deleting {server}:{export}/{remote_path}")

    mount_point = Path(tempfile.mkdtemp(prefix="backer_nfs_del_"))

    try:
        # Mount the NFS export
        nfs_opts = "soft,timeo=50,retrans=2"
        mount_cmd = ["mount", "-t", "nfs", "-o", nfs_opts, f"{server}:{export}", str(mount_point)]
        result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=30)

        # If mount fails, try with sudo
        if result.returncode != 0:
            mount_cmd = ["sudo", "-n", "mount", "-t", "nfs", "-o", nfs_opts, f"{server}:{export}", str(mount_point)]
            result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            return False, f"Failed to mount NFS: {result.stderr.strip()}"

        # Build full path to delete
        target_path = mount_point / remote_path

        if not target_path.exists():
            logger.info(f"[NFS DELETE] Path does not exist (already deleted): {remote_path}")
            return True, "Directory does not exist (already deleted)"

        # Delete the directory recursively
        try:
            shutil.rmtree(target_path)
            logger.info(f"[NFS DELETE] Successfully deleted: {server}:{export}/{remote_path}")
            return True, "Directory deleted successfully"
        except Exception as e:
            logger.error(f"[NFS DELETE] Failed to delete {remote_path}: {e}")
            return False, f"Failed to delete: {e}"

    except subprocess.TimeoutExpired:
        return False, "Mount timed out"
    except Exception as e:
        return False, str(e)

    finally:
        # Always try to unmount
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


def smb_delete_directory(
    server: str,
    share: str,
    remote_path: str,
    username: str | None = None,
    password: str | None = None,
    domain: str | None = None,
) -> tuple[bool, str]:
    """Recursively delete a directory on an SMB share.

    Performs a depth-first recursive deletion of all files and subdirectories.

    Args:
        server: SMB server hostname/IP
        share: Share name
        remote_path: Path to directory within share (MUST be non-empty)
        username: Optional username
        password: Optional password
        domain: Optional domain

    Returns:
        Tuple of (success, message)
    """
    remote_path = remote_path.replace("\\", "/").strip("/")

    # SAFETY: Refuse to delete share root - remote_path must be specified
    if not remote_path:
        logger.error("[SMB DELETE] Refusing to delete share root - remote_path is empty")
        return False, "Cannot delete share root - path must be specified"

    logger.info(f"[SMB DELETE] Deleting //{server}/{share}/{remote_path}")

    deleted_count = 0
    errors: list[str] = []

    def run_smb_command(commands: str, timeout: int = 30) -> tuple[int, str, str]:
        """Run smbclient with given commands."""
        with smb_auth_file(username, password, domain) as auth_path:
            cmd = smbclient_command(f"//{server}/{share}", "-t", "10")

            if auth_path:
                cmd.extend(["-A", auth_path])
            else:
                cmd.append("-N")

            cmd.extend(["-c", commands])

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
                return result.returncode, result.stdout, result.stderr
            except subprocess.TimeoutExpired:
                return -1, "", "Command timed out"
            except Exception as e:
                return -1, "", str(e)

    def delete_recursive(path: str) -> None:
        """Recursively delete a directory and its contents."""
        nonlocal deleted_count, errors

        # List directory contents - use SMBBrowser directly to get is_dir info
        success, result = SMBBrowser.list_directory(server, share, path, username, password, domain)
        if not success:
            # Log the error instead of silently returning
            error_msg = result if isinstance(result, str) else "Unknown error"
            if "not found" not in error_msg.lower():
                logger.warning(f"[SMB DELETE] Failed to list directory '{path}': {error_msg}")
                errors.append(f"list {path}: {error_msg[:50]}")
            return

        # Process entries - we now have DirectoryEntry objects with is_dir attribute
        for entry in result:
            if entry.name in (".", ".."):
                continue

            entry_path = f"{path}/{entry.name}"

            if entry.is_dir:
                # It's a directory - recurse first
                delete_recursive(entry_path)
                # Then delete the empty directory
                rc, _, err = run_smb_command(f'rmdir "{entry_path}"')
                if rc == 0 or "NT_STATUS_NO_SUCH_FILE" in err or "NT_STATUS_OBJECT_NAME_NOT_FOUND" in err:
                    deleted_count += 1
                    logger.debug(f"[SMB DELETE] Deleted directory: {entry_path}")
                elif "NT_STATUS_DIRECTORY_NOT_EMPTY" not in err:
                    errors.append(f"rmdir {entry_path}: {err.strip()[:50]}")
            else:
                # It's a file - delete it
                rc, _, err = run_smb_command(f'del "{entry_path}"')
                if rc == 0 or "NT_STATUS_NO_SUCH_FILE" in err or "NT_STATUS_OBJECT_NAME_NOT_FOUND" in err:
                    deleted_count += 1
                    logger.debug(f"[SMB DELETE] Deleted file: {entry_path}")
                else:
                    errors.append(f"del {entry_path}: {err.strip()[:50]}")

    try:
        # First, check if directory exists
        exists, check_ok = smb_file_exists(server, share, remote_path, username, password, domain)
        if check_ok and not exists:
            logger.info(f"[SMB DELETE] Path does not exist (already deleted): {remote_path}")
            return True, "Directory does not exist (already deleted)"

        # Recursively delete contents
        delete_recursive(remote_path)

        # Delete the target directory itself
        rc, _, err = run_smb_command(f'rmdir "{remote_path}"')
        if rc == 0 or "NT_STATUS_NO_SUCH_FILE" in err or "NT_STATUS_OBJECT_NAME_NOT_FOUND" in err:
            deleted_count += 1
            logger.info(f"[SMB DELETE] Successfully deleted: //{server}/{share}/{remote_path} ({deleted_count} items)")
            return True, f"Deleted {deleted_count} items"
        elif "NT_STATUS_DIRECTORY_NOT_EMPTY" in err:
            # Some files couldn't be deleted
            logger.warning(f"[SMB DELETE] Directory not empty after recursive delete: {remote_path}")
            if errors:
                return False, f"Deletion incomplete ({deleted_count} deleted): {'; '.join(errors[:3])}"
            return False, f"Directory not empty after recursive delete ({deleted_count} items deleted)"
        else:
            logger.error(f"[SMB DELETE] Failed to delete directory: {err.strip()}")
            return False, f"Failed to delete directory: {err.strip()}"

    except subprocess.TimeoutExpired:
        return False, "Command timed out"
    except Exception as e:
        logger.exception(f"Error in smb_delete_directory: {e}")
        return False, str(e)
