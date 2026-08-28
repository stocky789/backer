"""Tool manager for downloading and managing the Kopia binary."""

import hashlib
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
# Downloads are pinned and verified against the publishers' release checksum
# manifests. Signatures are not verified because this project does not ship the
# publishers' signing keys; an unavailable or mismatched checksum fails closed.
TOOL_INFO: dict[str, dict[str, Any]] = {
    "kopia": {
        "version": "0.23.1",
        "base_url": "https://github.com/kopia/kopia/releases/download/v{version}/kopia-{version}-{platform}-{arch}.{ext}",
        "checksum_url": "https://github.com/kopia/kopia/releases/download/v{version}/checksums.txt",
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
                [str(tool_path), "--version"],
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

    def _get_checksum_url(self, tool_name: str) -> str | None:
        info = TOOL_INFO.get(tool_name)
        if not info:
            return None
        return info["checksum_url"].format(version=info["version"])

    def _archive_name(self, tool_name: str) -> str:
        info = TOOL_INFO[tool_name]
        platform_info = info["platforms"][self._system]
        arch = info["arch_map"][self._machine]
        return info["base_url"].format(
            version=info["version"], platform=platform_info["name"], arch=arch,
            ext=platform_info.get("ext", ""),
        ).rsplit("/", 1)[-1]

    def download(self, tool_name: str, progress_callback: Any | None = None) -> Path:
        """Download and install a tool.

        Args:
            tool_name: Name of the tool to download
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
            download_path = tmpdir_path / self._archive_name(tool_name)
            self._download_file(url, download_path)
            self._verify_checksum(tool_name, download_path)

            extracted = self._extract_kopia(download_path, tmpdir_path, binary_name)

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

        # Never weaken TLS verification: tool downloads execute code locally.
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
        except Exception as e:
            raise RuntimeError(f"Failed to download {url}: {e}")

    def _verify_checksum(self, tool_name: str, archive_path: Path) -> None:
        checksum_url = self._get_checksum_url(tool_name)
        if not checksum_url:
            raise RuntimeError(f"No checksum manifest configured for {tool_name}")
        manifest_path = archive_path.with_name("checksums.txt")
        self._download_file(checksum_url, manifest_path)
        expected = None
        for line in manifest_path.read_text(encoding="utf-8", errors="replace").splitlines():
            fields = line.replace("*", " ").split()
            if len(fields) >= 2 and fields[-1] == archive_path.name:
                expected = fields[0].lower()
                break
        if not expected:
            raise RuntimeError(f"Checksum missing for {archive_path.name}")
        actual = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError(f"Checksum verification failed for {archive_path.name}")


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
