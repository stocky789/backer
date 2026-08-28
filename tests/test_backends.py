"""Tests for backup backends."""

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from backer.backends.base import BackendResult, BackupDestination, BackupSource, OperationType
from backer.backends.kopia import KopiaBackend
from backer.backends.proxy import ProxyBackend
from backer.backends.rclone import RcloneBackend
from backer.backends.registry import BackendRegistry, get_backend
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

    def test_get_backend_kopia(self) -> None:
        """Test getting kopia backend."""
        backend = get_backend("kopia")
        assert isinstance(backend, KopiaBackend)

    def test_get_backend_unknown(self) -> None:
        """Test getting unknown backend raises error."""
        with pytest.raises(ValueError, match="unknown_backend"):
            get_backend("unknown_backend")

    def test_available_backends(self) -> None:
        """Test listing available backends."""
        backends = BackendRegistry.available_backends()
        assert len(backends) >= 3  # At least rclone, restic, and kopia


class TestProxyBackend:
    def test_https_proxy_uses_standard_https_port_when_omitted(self) -> None:
        backend = ProxyBackend({"location": "proxys://backer.example.com/repo/repo-123"})

        assert backend.server_url == "https://backer.example.com"

    def test_proxy_preserves_an_explicit_public_port(self) -> None:
        backend = ProxyBackend({"location": "proxys://backer.example.com:8443/repo/repo-123"})

        assert backend.server_url == "https://backer.example.com:8443"

    def test_proxy_never_disables_tls_verification(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BACKER_SSL_VERIFY", "false")

        backend = ProxyBackend({"location": "proxys://backer.example.com/repo/repo-123"})

        assert backend.session.verify is True

    def test_proxy_maintenance_operations_do_not_make_requests(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = ProxyBackend({"location": "proxy://backer.example.com/repo/repo-123"})
        monkeypatch.setattr(
            backend, "_request", lambda *_args, **_kwargs: pytest.fail("unexpected network request")
        )
        destination = BackupDestination(path="proxy://backer.example.com/repo/repo-123")

        assert backend.list_snapshots(destination) == []
        results = [backend.init_repo(destination), backend.prune(destination), backend.check(destination)]

        assert [result.success for result in results] == [False, False, False]
        assert [result.errors for result in results] == [
            ["Proxy backend initialization is server-managed"],
            ["Proxy backend pruning is not supported by agent proxy capabilities"],
            ["Proxy backend integrity checks are not supported by agent proxy capabilities"],
        ]


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
        binary_path = Path('/usr/bin/rclone')

        with patch.object(backend, '_get_binary', return_value=binary_path):
            cmd = backend._build_command(
                operation="sync",
                source="/source",
                destination="/dest",
            )

        assert cmd[0] == str(binary_path)
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

    def test_restore_rejects_historical_snapshot(self) -> None:
        result = RcloneBackend().restore(
            BackupDestination(path="/backup"), Path("/restore"), snapshot="abc123"
        )

        assert not result.success
        assert "current state only" in result.errors[0]


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
        binary_path = Path('/usr/bin/restic')

        with patch.object(backend, '_get_binary', return_value=binary_path):
            cmd = backend._build_backup_command(
                repo="/backup/repo",
                source="/data",
            )

        assert cmd[0] == str(binary_path)
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


class TestKopiaBackend:
    """Tests for kopia backend."""

    def test_repository_password_from_config(self) -> None:
        """Kopia only accepts the repository password boundary key."""
        backend = KopiaBackend(config={"repository_password": "test_password"})
        assert backend._env.get("KOPIA_PASSWORD") == "test_password"

    def test_connect_requires_repository_password_before_kopia_runs(self) -> None:
        success, message = KopiaBackend()._connect_repo("/backup/repo")
        assert not success
        assert message == "Repository encryption password is required"

    def test_restore_dry_run_is_rejected_without_running_kopia(self) -> None:
        result = KopiaBackend().restore(
            BackupDestination(path="/backup"), Path("/restore"), dry_run=True
        )

        assert not result.success
        assert result.errors == ["Kopia restore dry runs are not supported"]

    def test_get_repo_type_filesystem(self) -> None:
        """Test filesystem repository type detection."""
        backend = KopiaBackend()
        repo_type, args = backend._get_repo_type("/backup/repo")
        assert repo_type == "filesystem"
        assert "--path" in args
        assert "/backup/repo" in args

    def test_get_repo_type_s3(self) -> None:
        """Test S3 repository type detection."""
        backend = KopiaBackend({"s3": {
            "bucket": "mybucket", "prefix": "prefix", "endpoint": "https://minio.test",
            "region": "us-east-1", "access_key_id": "access", "secret_access_key": "secret",
        }})
        repo_type, args = backend._get_repo_type("s3://mybucket/prefix")
        assert repo_type == "s3"
        assert "--bucket" in args
        assert "mybucket" in args

    def test_get_repo_type_gcs(self) -> None:
        """Test GCS repository type detection."""
        backend = KopiaBackend()
        repo_type, args = backend._get_repo_type("gs://mybucket/prefix")
        assert repo_type == "gcs"
        assert "--bucket" in args

    def test_get_repo_type_azure(self) -> None:
        """Test Azure repository type detection."""
        backend = KopiaBackend()
        repo_type, args = backend._get_repo_type("azure://mycontainer/prefix")
        assert repo_type == "azure"
        assert "--container" in args

    def test_get_repo_type_sftp(self) -> None:
        """Test SFTP repository type detection."""
        backend = KopiaBackend()
        repo_type, args = backend._get_repo_type("sftp://server/path")
        assert repo_type == "sftp"
        assert "--path" in args

    def test_get_repo_type_invalid_s3_path(self) -> None:
        """S3 locations require managed S3 configuration."""
        backend = KopiaBackend()
        with pytest.raises(ValueError, match="S3 repository configuration is required"):
            backend._get_repo_type("s3://bucket")

    def test_get_repo_type_invalid_azure_path(self) -> None:
        """Test that invalid Azure path raises ValueError."""
        backend = KopiaBackend()
        with pytest.raises(ValueError, match="container name is required"):
            backend._get_repo_type("azure://")

    def test_get_repo_type_empty_path(self) -> None:
        """Test that empty path raises ValueError."""
        backend = KopiaBackend()
        with pytest.raises(ValueError, match="cannot be empty"):
            backend._get_repo_type("")
