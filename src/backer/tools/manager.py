"""Tool manager for downloading and managing backup tool binaries.

Automatically downloads rclone, restic, etc. so users don't need to install them manually.
"""

import platform
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from backer import __version__

# Tool download information
# Note: rsync backend exists but is NOT currently supported for remote agents.
# Only rclone and restic are supported for agent-based backups.
TOOL_INFO: dict[str, dict[str, Any]] = {
    "rclone": {
        "version": "1.72.1",
        "base_url": "https://downloads.rclone.org/v{version}/rclone-v{version}-{platform}-{arch}.{ext}",
        "platforms": {
            "Linux": {"name": "linux", "ext": "zip"},
            "Darwin": {"name": "osx", "ext": "zip"},
            "Windows": {"name": "windows", "ext": "zip"},
        },
        "arch_map": {
            "x86_64": "amd64",
            "AMD64": "amd64",
            "aarch64": "arm64",
            "arm64": "arm64",
        },
        "binary_name": {"Linux": "rclone", "Darwin": "rclone", "Windows": "rclone.exe"},
    },
    "restic": {
        "version": "0.17.3",
        "base_url": "https://github.com/restic/restic/releases/download/v{version}/restic_{version}_{platform}_{arch}.bz2",
        "platforms": {
            "Linux": {"name": "linux"},
            "Darwin": {"name": "darwin"},
            "Windows": {"name": "windows"},
        },
        "arch_map": {
            "x86_64": "amd64",
            "AMD64": "amd64",
            "aarch64": "arm64",
            "arm64": "arm64",
        },
        "binary_name": {"Linux": "restic", "Darwin": "restic", "Windows": "restic.exe"},
    },
    "kopia": {
        "version": "0.22.3",
        "base_url": "https://github.com/kopia/kopia/releases/download/v{version}/kopia-{version}-{platform}-{arch}.{ext}",
        "platforms": {
            "Linux": {"name": "linux", "ext": "tar.gz"},
            "Darwin": {"name": "macOS", "ext": "tar.gz"},
            "Windows": {"name": "windows", "ext": "zip"},
        },
        "arch_map": {
            "x86_64": "x64",
            "AMD64": "x64",
            "aarch64": "arm64",
            "arm64": "arm64",
        },
        "binary_name": {"Linux": "kopia", "Darwin": "kopia", "Windows": "kopia.exe"},
    },
    # rsync is NOT supported for agent backups - only for local server-side operations
}


class ToolManager:
    """Manages downloading and updating backup tools."""

    def __init__(self, tools_dir: Path | None = None):
        """Initialize the tool manager.

        Args:
            tools_dir: Directory to store tool binaries.
                      Defaults to $BACKER_DATA_DIR/tools or ~/.local/share/backer/tools/
        """
        if tools_dir is None:
            # Check for BACKER_DATA_DIR environment variable first
            import os
            data_dir_env = os.environ.get("BACKER_DATA_DIR")
            if data_dir_env:
                tools_dir = Path(data_dir_env) / "tools"
            else:
                tools_dir = Path.home() / ".local" / "share" / "backer" / "tools"
        self.tools_dir = tools_dir
        self.tools_dir.mkdir(parents=True, exist_ok=True)

        self._system = platform.system()
        self._machine = platform.machine()

    def get_tool_path(self, tool_name: str) -> Path | None:
        """Get the path to a tool binary.

        Returns the path if the tool is installed, None otherwise.
        Checks multiple locations for Linux to handle systemd service context.
        """
        if tool_name not in TOOL_INFO:
            return None

        info = TOOL_INFO[tool_name]
        binary_name = info["binary_name"].get(self._system)
        if not binary_name:
            return None

        # Check the configured tools directory first
        tool_path = self.tools_dir / binary_name
        if tool_path.exists():
            return tool_path

        # On Linux, also check /root/.local/share/backer/tools/ (for systemd service)
        # This handles cases where HOME might not be set correctly
        if self._system == "Linux":
            try:
                root_tools_path = Path("/root/.local/share/backer/tools") / binary_name
                if root_tools_path.exists():
                    return root_tools_path
            except PermissionError:
                pass  # Can't access /root, skip this check

            try:
                # Also check /opt/backer/tools/ as a fallback system-wide location
                opt_tools_path = Path("/opt/backer/tools") / binary_name
                if opt_tools_path.exists():
                    return opt_tools_path
            except PermissionError:
                pass  # Can't access /opt/backer, skip this check

        # Check if tool is available system-wide
        system_path = shutil.which(tool_name)
        if system_path:
            return Path(system_path)

        return None

    def is_installed(self, tool_name: str) -> bool:
        """Check if a tool is installed (either managed or system-wide)."""
        return self.get_tool_path(tool_name) is not None

    def get_version(self, tool_name: str) -> str | None:
        """Get the installed version of a tool."""
        tool_path = self.get_tool_path(tool_name)
        if not tool_path:
            return None

        try:
            result = subprocess.run(
                [str(tool_path), "version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip().split("\n")[0]
        except Exception:
            return None

    def _get_download_url(self, tool_name: str) -> str | None:
        """Get the download URL for a tool."""
        if tool_name not in TOOL_INFO:
            return None

        info = TOOL_INFO[tool_name]

        # Get platform info
        platform_info = info["platforms"].get(self._system)
        if not platform_info:
            return None

        # Get architecture
        arch = info["arch_map"].get(self._machine)
        if not arch:
            return None

        # Build URL
        url = info["base_url"].format(
            version=info["version"],
            platform=platform_info["name"],
            arch=arch,
            ext=platform_info.get("ext", ""),
        )

        return url

    def download(self, tool_name: str, progress_callback: Any | None = None) -> Path:
        """Download and install a tool.

        Args:
            tool_name: Name of the tool to download (rclone, restic)
            progress_callback: Optional callback for progress updates

        Returns:
            Path to the installed binary

        Raises:
            ValueError: If tool is not supported
            RuntimeError: If download or installation fails
        """
        if tool_name not in TOOL_INFO:
            raise ValueError(f"Unknown tool: {tool_name}. Supported: {list(TOOL_INFO.keys())}")

        url = self._get_download_url(tool_name)
        if not url:
            raise RuntimeError(
                f"No download available for {tool_name} on {self._system}/{self._machine}"
            )

        info = TOOL_INFO[tool_name]
        binary_name = info["binary_name"][self._system]
        target_path = self.tools_dir / binary_name

        if progress_callback:
            progress_callback(f"Downloading {tool_name} from {url}")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            # Download the file
            download_path = tmpdir_path / f"{tool_name}_download"
            self._download_file(url, download_path)

            # Extract based on tool type
            if tool_name == "rclone":
                extracted = self._extract_rclone(download_path, tmpdir_path)
            elif tool_name == "restic":
                extracted = self._extract_restic(download_path, tmpdir_path, binary_name)
            elif tool_name == "kopia":
                extracted = self._extract_kopia(download_path, tmpdir_path, binary_name)
            else:
                raise RuntimeError(f"Don't know how to extract {tool_name}")

            # Move to final location
            shutil.move(str(extracted), str(target_path))

            # Make executable
            target_path.chmod(target_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        if progress_callback:
            progress_callback(f"Installed {tool_name} to {target_path}")

        return target_path

    def _download_file(self, url: str, dest: Path) -> None:
        """Download a file from URL."""
        import ssl

        # Create request with User-Agent (GitHub blocks requests without one)
        request = Request(
            url,
            headers={"User-Agent": f"Backer-Agent/{__version__}"}
        )

        # Try with default SSL first, fall back to unverified for Windows cert issues
        ssl_context = None
        try:
            ssl_context = ssl.create_default_context()
            # Try to use certifi certs if available (more reliable on Windows)
            try:
                import certifi
                ssl_context.load_verify_locations(certifi.where())
            except ImportError:
                pass
        except Exception:
            pass

        try:
            with urlopen(request, timeout=120, context=ssl_context) as response:
                with open(dest, "wb") as f:
                    shutil.copyfileobj(response, f)
        except ssl.SSLCertVerificationError:
            # Windows often has certificate issues - use unverified context
            # This is acceptable since we're downloading from known trusted URLs
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            with urlopen(request, timeout=120, context=ssl_context) as response:
                with open(dest, "wb") as f:
                    shutil.copyfileobj(response, f)
        except Exception as e:
            raise RuntimeError(f"Failed to download {url}: {e}")

    def _extract_rclone(self, archive_path: Path, tmpdir: Path) -> Path:
        """Extract rclone from zip archive."""
        with zipfile.ZipFile(archive_path, "r") as zf:
            # Find the rclone binary in the archive
            for name in zf.namelist():
                if name.endswith("/rclone") or name.endswith("/rclone.exe"):
                    zf.extract(name, tmpdir)
                    return tmpdir / name

        raise RuntimeError("Could not find rclone binary in archive")

    def _extract_restic(self, archive_path: Path, tmpdir: Path, binary_name: str) -> Path:
        """Extract restic from bz2 archive."""
        import bz2

        extracted_path = tmpdir / binary_name

        with bz2.open(archive_path, "rb") as f_in:
            with open(extracted_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)

        return extracted_path

    def _extract_kopia(self, archive_path: Path, tmpdir: Path, binary_name: str) -> Path:
        """Extract kopia from tar.gz or zip archive."""
        import tarfile

        # Try tar.gz first (Linux/macOS)
        if str(archive_path).endswith(".tar.gz") or tarfile.is_tarfile(str(archive_path)):
            try:
                with tarfile.open(archive_path, "r:gz") as tf:
                    # Find the kopia binary in the archive
                    for member in tf.getmembers():
                        if member.name.endswith("/kopia") or member.name == "kopia":
                            tf.extract(member, tmpdir)
                            return tmpdir / member.name
                raise RuntimeError("Could not find kopia binary in tar archive")
            except tarfile.TarError:
                pass

        # Try zip (Windows)
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                for name in zf.namelist():
                    if name.endswith("/kopia.exe") or name == "kopia.exe":
                        zf.extract(name, tmpdir)
                        return tmpdir / name
            raise RuntimeError("Could not find kopia binary in zip archive")
        except zipfile.BadZipFile:
            raise RuntimeError("Could not extract kopia - unsupported archive format")

    def ensure_installed(
        self, tool_name: str, progress_callback: Any | None = None
    ) -> Path:
        """Ensure a tool is installed, downloading if necessary.

        Returns:
            Path to the tool binary
        """
        existing = self.get_tool_path(tool_name)
        if existing:
            return existing

        return self.download(tool_name, progress_callback)

    def list_tools(self) -> dict[str, dict[str, Any]]:
        """List all supported tools and their status."""
        result = {}
        for tool_name in TOOL_INFO:
            path = self.get_tool_path(tool_name)
            result[tool_name] = {
                "installed": path is not None,
                "path": str(path) if path else None,
                "version": self.get_version(tool_name) if path else None,
                "managed": path and self.tools_dir in path.parents if path else False,
                "available_version": TOOL_INFO[tool_name]["version"],
            }
        return result

    def update(self, tool_name: str, progress_callback: Any | None = None) -> Path:
        """Update a tool to the latest bundled version."""
        # Remove existing managed version
        info = TOOL_INFO.get(tool_name)
        if info:
            binary_name = info["binary_name"].get(self._system)
            if binary_name:
                existing = self.tools_dir / binary_name
                if existing.exists():
                    existing.unlink()

        # Download fresh
        return self.download(tool_name, progress_callback)


# Global tool manager instance
_tool_manager: ToolManager | None = None


def get_tool_manager() -> ToolManager:
    """Get the global tool manager instance."""
    global _tool_manager
    if _tool_manager is None:
        _tool_manager = ToolManager()
    return _tool_manager


def get_tool_path(tool_name: str) -> Path | None:
    """Convenience function to get tool path."""
    return get_tool_manager().get_tool_path(tool_name)
