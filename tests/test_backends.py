"""Tests for backup backends."""

import pytest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch, MagicMock
import subprocess

from backer.backends.base import BackendResult, BackupSource, BackupDestination, OperationType
from backer.backends.registry import BackendRegistry, get_backend
from backer.backends.rclone import RcloneBackend
from backer.backends.restic import ResticBackend


class TestBackendRegistry:
    """Tests for the backend registry."""

    def test_get_backend_rclone(self) -> None:
        """Test getting rclone backend."""
        backend = get_backend("rclone")
        assert isinstance(backend, RcloneBackend)

    def test_get_backend_restic(self) -> None:
        """Test getting restic backend."""
        backend = get_backend("restic")
        assert isinstance(backend, ResticBackend)

    def test_get_backend_unknown(self) -> None:
        """Test getting unknown backend raises error."""
        with pytest.raises(ValueError, match="Unknown backend"):
            get_backend("unknown_backend")

    def test_available_backends(self) -> None:
        """Test listing available backends."""
        backends = BackendRegistry.available_backends()
        assert len(backends) >= 2  # At least rclone and restic


class TestBackendResult:
    """Tests for BackendResult dataclass."""

    def test_duration_seconds(self) -> None:
        """Test duration calculation."""
        start = datetime(2024, 1, 1, 12, 0, 0)
        end = datetime(2024, 1, 1, 12, 1, 30)

        result = BackendResult(
            success=True,
            operation=OperationType.BACKUP,
            started_at=start,
            finished_at=end,
        )

        assert result.duration_seconds == 90.0

    def test_default_values(self) -> None:
        """Test default values are set correctly."""
        result = BackendResult(
            success=True,
            operation=OperationType.BACKUP,
            started_at=datetime.now(),
            finished_at=datetime.now(),
        )

        assert result.bytes_transferred == 0
        assert result.files_transferred == 0
        assert result.errors == []
        assert result.warnings == []
        assert result.output == ""
        assert result.return_code == 0


class TestRcloneBackend:
    """Tests for rclone backend."""

    def test_build_command_basic(self) -> None:
        """Test basic command building."""
        backend = RcloneBackend()

        with patch.object(backend, '_get_binary', return_value=Path('/usr/bin/rclone')):
            cmd = backend._build_command(
                operation="sync",
                source="/source",
                destination="/dest",
            )

        assert cmd[0] == "/usr/bin/rclone"
        assert cmd[1] == "sync"
        assert "/source" in cmd
        assert "/dest" in cmd

    def test_build_command_with_excludes(self) -> None:
        """Test command building with excludes."""
        backend = RcloneBackend()

        with patch.object(backend, '_get_binary', return_value=Path('/usr/bin/rclone')):
            cmd = backend._build_command(
                operation="sync",
                source="/source",
                destination="/dest",
                excludes=["*.tmp", ".cache"],
            )

        assert "--exclude" in cmd
        assert "*.tmp" in cmd
        assert ".cache" in cmd

    def test_build_command_dry_run(self) -> None:
        """Test command building with dry run."""
        backend = RcloneBackend()

        with patch.object(backend, '_get_binary', return_value=Path('/usr/bin/rclone')):
            cmd = backend._build_command(
                operation="sync",
                source="/source",
                destination="/dest",
                dry_run=True,
            )

        assert "--dry-run" in cmd


class TestResticBackend:
    """Tests for restic backend."""

    def test_password_from_config(self) -> None:
        """Test password is set from config."""
        backend = ResticBackend(config={"password": "test_password"})
        assert backend._env.get("RESTIC_PASSWORD") == "test_password"

    def test_password_file_from_config(self) -> None:
        """Test password file is set from config."""
        backend = ResticBackend(config={"password_file": "/path/to/password"})
        assert backend._env.get("RESTIC_PASSWORD_FILE") == "/path/to/password"

    def test_build_backup_command(self) -> None:
        """Test backup command building."""
        backend = ResticBackend()

        with patch.object(backend, '_get_binary', return_value=Path('/usr/bin/restic')):
            cmd = backend._build_backup_command(
                repo="/backup/repo",
                source="/data",
            )

        assert cmd[0] == "/usr/bin/restic"
        assert "backup" in cmd
        assert "--repo" in cmd
        assert "/backup/repo" in cmd
        assert "/data" in cmd
        assert "--json" in cmd

    def test_build_backup_command_with_excludes(self) -> None:
        """Test backup command with excludes."""
        backend = ResticBackend()

        with patch.object(backend, '_get_binary', return_value=Path('/usr/bin/restic')):
            cmd = backend._build_backup_command(
                repo="/backup/repo",
                source="/data",
                excludes=["*.log", "temp/"],
            )

        assert "--exclude" in cmd
        exclude_indices = [i for i, x in enumerate(cmd) if x == "--exclude"]
        assert len(exclude_indices) == 2


class TestBackupSource:
    """Tests for BackupSource."""

    def test_basic_source(self) -> None:
        """Test basic source creation."""
        source = BackupSource(path=Path("/data"))
        assert source.path == Path("/data")
        assert source.excludes == []
        assert source.includes == []

    def test_source_with_patterns(self) -> None:
        """Test source with exclude/include patterns."""
        source = BackupSource(
            path=Path("/data"),
            excludes=["*.tmp", ".cache"],
            includes=["*.py"],
        )
        assert "*.tmp" in source.excludes
        assert "*.py" in source.includes


class TestBackupDestination:
    """Tests for BackupDestination."""

    def test_local_destination(self) -> None:
        """Test local path destination."""
        dest = BackupDestination(path="/backup/data")
        assert dest.path == "/backup/data"

    def test_remote_destination(self) -> None:
        """Test remote URI destination."""
        dest = BackupDestination(path="s3:bucket/path")
        assert dest.path == "s3:bucket/path"
