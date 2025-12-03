"""Tests for the tool manager."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from backer.tools.manager import TOOL_INFO, ToolManager


class TestToolManager:
    """Tests for ToolManager class."""

    def test_init_creates_tools_dir(self, tmp_path: Path) -> None:
        """Test that ToolManager creates the tools directory."""
        tools_dir = tmp_path / "tools"
        _manager = ToolManager(tools_dir)  # noqa: F841
        assert tools_dir.exists()

    def test_get_tool_path_not_installed(self, tmp_path: Path) -> None:
        """Test get_tool_path returns None when tool not installed."""
        manager = ToolManager(tmp_path / "tools")
        with patch("shutil.which", return_value=None):
            assert manager.get_tool_path("rclone") is None

    def test_get_tool_path_system_installed(self, tmp_path: Path) -> None:
        """Test get_tool_path returns system path when available."""
        manager = ToolManager(tmp_path / "tools")
        with patch("shutil.which", return_value="/usr/bin/rclone"):
            path = manager.get_tool_path("rclone")
            assert path == Path("/usr/bin/rclone")

    def test_get_tool_path_managed(self, tmp_path: Path) -> None:
        """Test get_tool_path returns managed path when installed."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()

        # Create a fake rclone binary (with .exe on Windows)
        binary_name = "rclone.exe" if sys.platform == "win32" else "rclone"
        rclone_path = tools_dir / binary_name
        rclone_path.write_text("#!/bin/bash\necho rclone")

        manager = ToolManager(tools_dir)
        path = manager.get_tool_path("rclone")
        assert path == rclone_path

    def test_is_installed(self, tmp_path: Path) -> None:
        """Test is_installed method."""
        manager = ToolManager(tmp_path / "tools")

        with patch("shutil.which", return_value=None):
            assert not manager.is_installed("rclone")

        with patch("shutil.which", return_value="/usr/bin/rclone"):
            assert manager.is_installed("rclone")

    def test_list_tools(self, tmp_path: Path) -> None:
        """Test list_tools returns all supported tools."""
        manager = ToolManager(tmp_path / "tools")

        with patch("shutil.which", return_value=None):
            tools = manager.list_tools()

            assert "rclone" in tools
            assert "restic" in tools

            for name, info in tools.items():
                assert "installed" in info
                assert "available_version" in info

    def test_get_download_url_linux_amd64(self, tmp_path: Path) -> None:
        """Test URL generation for Linux AMD64."""
        manager = ToolManager(tmp_path / "tools")
        manager._system = "Linux"
        manager._machine = "x86_64"

        url = manager._get_download_url("rclone")
        assert url is not None
        assert "linux" in url
        assert "amd64" in url

    def test_get_download_url_unsupported_platform(self, tmp_path: Path) -> None:
        """Test URL returns None for unsupported platform."""
        manager = ToolManager(tmp_path / "tools")
        manager._system = "UnknownOS"
        manager._machine = "unknown_arch"

        url = manager._get_download_url("rclone")
        assert url is None

    def test_unknown_tool(self, tmp_path: Path) -> None:
        """Test handling of unknown tool names."""
        manager = ToolManager(tmp_path / "tools")

        assert manager.get_tool_path("unknown_tool") is None

        with pytest.raises(ValueError, match="Unknown tool"):
            manager.download("unknown_tool")


class TestToolInfo:
    """Tests for tool information constants."""

    def test_rclone_info_complete(self) -> None:
        """Test rclone info has all required fields."""
        info = TOOL_INFO["rclone"]
        assert "version" in info
        assert "base_url" in info
        assert "platforms" in info
        assert "arch_map" in info
        assert "binary_name" in info

    def test_restic_info_complete(self) -> None:
        """Test restic info has all required fields."""
        info = TOOL_INFO["restic"]
        assert "version" in info
        assert "base_url" in info
        assert "platforms" in info
        assert "arch_map" in info
        assert "binary_name" in info

    def test_supported_platforms(self) -> None:
        """Test all tools support major platforms."""
        for tool_name, info in TOOL_INFO.items():
            platforms = info["platforms"]
            assert "Linux" in platforms, f"{tool_name} missing Linux support"
            # Darwin and Windows are nice to have but not required
