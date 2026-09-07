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
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_SMB_CONF = "/etc/samba/smb.conf"

# A kopia repository lives in a folder holding one of these markers.
_REPO_MARKERS = ("kopia.repository", "kopia.repository.f", ".backer")


def smbclient_command(*args: str) -> list[str]:
    """Build an smbclient argv that starts even when the distro ships no smb.conf.

    smbclient refuses to run when /etc/samba/smb.conf is missing (Arch, minimal
    containers). Point it at an empty config only in that case, so a host with a
    tuned smb.conf keeps its settings.
    """
    command = ["smbclient", *args]
    if sys.platform != "win32" and not os.path.exists(_DEFAULT_SMB_CONF):
        command.append("--configfile=/dev/null")
    return command


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


def windows_smb_error(text: str) -> str:
    """Map `net use` / `net view` failure text onto the Linux branch's wording."""
    lowered = text.lower()
    if "1326" in lowered or "user name or password is incorrect" in lowered or "logon failure" in lowered:
        return "Login failed - invalid username or password"
    if "system error 5 " in lowered or "access is denied" in lowered:
        return "Access denied - check credentials"
    if "system error 53" in lowered or "system error 67" in lowered or "network path was not found" in lowered:
        return "Server not found or not accessible"
    if "system error 1231" in lowered or "network location cannot be reached" in lowered:
        return "Host unreachable - check network connection"
    if "1219" in lowered:
        return "Windows already has a session to this server with different credentials - disconnect it first"
    return text.strip() or "Unknown error connecting to server"


def parse_net_view(output: str) -> list["ShareInfo"]:
    """Pull the Disk rows out of `net view \\\\server`, skipping hidden shares."""
    shares: list[ShareInfo] = []
    for line in output.splitlines():
        # Columns are fixed-width, so the name ends at the run of 2+ spaces before
        # the type column - share names may contain spaces ("Virtual Machines").
        match = re.match(r"^(.+?)\s{2,}Disk\b\s*(.*)$", line)
        if not match:
            continue
        name = match.group(1)
        if name.endswith("$"):
            continue
        # Drop the "Used as" drive letter when the share is already mapped.
        comment = re.sub(r"^[A-Za-z]:\s*", "", match.group(2).strip())
        shares.append(ShareInfo(name=name, share_type="Disk", comment=comment))
    return shares


def _parse_smbclient_listing(output: str) -> list[tuple[str, bool, int, str]]:
    """Yield (name, is_dir, size, modified) from an smbclient `ls`, dropping "." and ".."."""
    rows: list[tuple[str, bool, int, str]] = []
    for line in output.split("\n"):
        line = line.strip()
        # "  name        D        0  Wed Dec  3 10:15:30 2025" (dir) or the same with no D (file)
        match = re.match(r"^\s*(.+?)\s+([DAHNRS]*)\s+(\d+)\s+(.+)$", line)
        if not match:
            continue
        name = match.group(1).strip()
        if name in (".", ".."):
            continue
        rows.append((name, "D" in match.group(2), int(match.group(3)), match.group(4).strip()))
    return rows


def _parse_smbclient_dirs(output: str) -> list[str]:
    """Directory names only from an smbclient `ls`."""
    return [name for name, is_dir, _size, _mod in _parse_smbclient_listing(output) if is_dir]


def _listing_has_marker(output: str) -> bool:
    """True when an smbclient `ls` listing contains a kopia repository marker."""
    names = {name for name, _is_dir, _size, _mod in _parse_smbclient_listing(output)}
    return any(marker in names for marker in _REPO_MARKERS)


def _local_dir_is_repository(path: Path) -> bool:
    """Best-effort shallow check for a kopia marker inside a local/UNC directory."""
    try:
        return any((path / marker).exists() for marker in _REPO_MARKERS)
    except OSError:
        return False


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
    is_repository: bool = False


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
        if sys.platform == "win32":
            return SMBBrowser._list_shares_windows(server, username, password, domain)

        with smb_auth_file(username, password, domain) as auth_path:
            # Build smbclient command with connection timeout
            # -g for parseable output, 5s connection timeout
            cmd = smbclient_command("-L", f"//{server}", "-g", "-t", "5")

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
                    error = result.stderr.strip() or result.stdout.strip()
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
    def _list_shares_windows(
        server: str,
        username: str | None,
        password: str | None,
        domain: str | None,
    ) -> tuple[bool, list[ShareInfo] | str]:
        """Windows has no smbclient: authenticate with `net use`, enumerate with `net view`."""
        from backer.core.mounts import SMBConnectionManager, get_subprocess_flags

        manager = SMBConnectionManager()
        created = False
        if username and password:
            failures: list[str] = []

            def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                completed = subprocess.run(argv, **kwargs)  # type: ignore[arg-type]
                if completed.returncode:
                    failures.append(completed.stderr or completed.stdout or "")
                return completed

            # Password reaches `net use` on stdin only - never argv.
            created = manager.connect_with_stdin(
                server, "IPC$", f"{domain}\\{username}" if domain else username, password, runner
            )
            if not created:
                return False, windows_smb_error(failures[0] if failures else "")

        try:
            result = subprocess.run(
                ["net", "view", f"\\\\{server}"],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=get_subprocess_flags(),
            )
            if result.returncode != 0:
                return False, windows_smb_error(result.stderr.strip() or result.stdout.strip())
            return True, parse_net_view(result.stdout)
        except subprocess.TimeoutExpired:
            return False, "Connection timed out"
        except Exception as error:  # pragma: no cover - defensive
            return False, str(error)
        finally:
            if created:
                manager.disconnect_serverless(server, "IPC$")

    @staticmethod
    def list_directory(
        server: str,
        share: str,
        path: str = "",
        username: str | None = None,
        password: str | None = None,
        domain: str | None = None,
        directories_only: bool = False,
    ) -> tuple[bool, list[DirectoryEntry] | str]:
        """List the contents of a path on an SMB share.

        By default returns files and directories with sizes (the folder/file
        browsers rely on this). With ``directories_only=True`` returns only
        subdirectories, each flagged with ``is_repository`` when it already holds
        a kopia marker - the repository-folder picker uses this form.

        Returns:
            Tuple of (success, entries_list or error_message)
        """
        path = path.replace("\\", "/").strip("/") if path else ""
        if sys.platform == "win32":
            return SMBBrowser._list_directory_windows(
                server, share, path, username, password, domain, directories_only
            )

        with smb_auth_file(username, password, domain) as auth_path:
            result = SMBBrowser._smbclient_ls(server, share, auth_path, path)
            if result is None:
                return False, "Connection timed out"
            if result.returncode != 0:
                error = result.stderr.strip() or result.stdout.strip()
                if "NT_STATUS_LOGON_FAILURE" in error:
                    return False, "Login failed - invalid username or password"
                elif "NT_STATUS_ACCESS_DENIED" in error:
                    return False, "Access denied to this directory"
                elif "NT_STATUS_BAD_NETWORK_NAME" in error:
                    return False, "Server not found or not accessible"
                elif "NT_STATUS_HOST_UNREACHABLE" in error:
                    return False, "Host unreachable - check network connection"
                elif "NT_STATUS_OBJECT_NAME_NOT_FOUND" in error:
                    return False, "Directory not found"
                else:
                    return False, error or "Failed to list directory"

            entries = []
            for name, is_dir, size, modified in _parse_smbclient_listing(result.stdout):
                if directories_only and not is_dir:
                    continue
                # Probing each subdir for a marker costs an extra ls, so only do it
                # for the repository picker, where the flag is what it is asking for.
                is_repo = False
                if directories_only and is_dir:
                    sub = f"{path}/{name}" if path else name
                    is_repo = SMBBrowser._smb_dir_is_repository(server, share, auth_path, sub)
                entries.append(
                    DirectoryEntry(name=name, is_dir=is_dir, size=size, modified=modified, is_repository=is_repo)
                )
            entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
            return True, entries

    @staticmethod
    def _smbclient_ls(
        server: str, share: str, auth_path: str | None, smb_path: str
    ) -> subprocess.CompletedProcess[str] | None:
        """Run a single smbclient ``ls`` in ``smb_path``; None on spawn/timeout failure."""
        cmd = smbclient_command(f"//{server}/{share}", "-t", "5")
        cmd.extend(["-A", auth_path] if auth_path else ["-N"])
        # cd + ls handles paths containing spaces better than a quoted-wildcard ls.
        cmd.extend(["-c", f'cd "/{smb_path}"; ls' if smb_path else "ls"])
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None

    @staticmethod
    def _smb_dir_is_repository(server: str, share: str, auth_path: str | None, smb_path: str) -> bool:
        """Best-effort shallow check: does this subdir directly hold a kopia marker?"""
        result = SMBBrowser._smbclient_ls(server, share, auth_path, smb_path)
        if result is None or result.returncode != 0:
            return False
        return _listing_has_marker(result.stdout)

    @staticmethod
    def _list_directory_windows(
        server: str,
        share: str,
        path: str,
        username: str | None,
        password: str | None,
        domain: str | None,
        directories_only: bool = False,
    ) -> tuple[bool, list[DirectoryEntry] | str]:
        """Windows has no smbclient: authenticate with `net use`, enumerate with pathlib."""
        from backer.core.mounts import SMBConnectionManager

        manager = SMBConnectionManager()
        created = False
        if username and password:
            failures: list[str] = []

            def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                completed = subprocess.run(argv, **kwargs)  # type: ignore[arg-type]
                if completed.returncode:
                    failures.append(completed.stderr or completed.stdout or "")
                return completed

            # Password reaches `net use` on stdin only - never argv.
            created = manager.connect_with_stdin(
                server, "IPC$", f"{domain}\\{username}" if domain else username, password, runner
            )
            if not created:
                return False, windows_smb_error(failures[0] if failures else "")

        try:
            base = Path(rf"\\{server}\{share}")
            if path:
                base = base / path.replace("/", "\\")
            entries = []
            for item in base.iterdir():
                try:
                    is_dir = item.is_dir()
                except OSError:
                    continue
                if directories_only and not is_dir:
                    continue
                size = 0
                if not is_dir:
                    try:
                        size = item.stat().st_size
                    except OSError:
                        size = 0
                is_repo = directories_only and is_dir and _local_dir_is_repository(item)
                entries.append(DirectoryEntry(name=item.name, is_dir=is_dir, size=size, is_repository=is_repo))
            entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
            return True, entries
        except PermissionError:
            return False, "Access denied to this directory"
        except FileNotFoundError:
            return False, "Directory not found"
        except OSError as error:
            return False, windows_smb_error(str(error))
        finally:
            if created:
                manager.disconnect_serverless(server, "IPC$")

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
            command = smbclient_command(f"//{server}/{share}", "-t", "5")
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
