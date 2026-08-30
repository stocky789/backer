"""Tests for backup backends."""

from datetime import datetime
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from backer.backends.base import BackendResult, BackendType, BackupDestination, BackupSource, OperationType
from backer.backends.kopia import KopiaBackend
from backer.backends.proxy import ProxyBackend
from backer.backends.registry import BackendRegistry, get_backend


class TestBackendRegistry:
    """Tests for the backend registry."""

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
        assert set(backends) == {BackendType.KOPIA, BackendType.PROXY}


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
            ["No retention policy configured - refusing to prune. Nothing was deleted."],
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

    def test_backup_replaces_kopia_ignore_policy_for_changed_or_removed_excludes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        backend = KopiaBackend({"repository_password": "test-password"})
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
        monkeypatch.setattr(backend, "_connect_repo", lambda _: (True, "connected"))

        def run(command: list[str], **_: object) -> CompletedProcess[str]:
            calls.append(command)
            return CompletedProcess(command, 0, '{"id":"snapshot"}\n', "")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        for excludes in (["*.one"], ["*.two"], []):
            assert backend.backup(BackupSource(tmp_path, excludes=excludes), BackupDestination("repo")).success

        policies = [call for call in calls if call[1:3] == ["policy", "set"]]
        assert policies == [
            ["kopia", "policy", "set", str(tmp_path), "--clear-ignore", "--add-ignore", "*.one"],
            ["kopia", "policy", "set", str(tmp_path), "--clear-ignore", "--add-ignore", "*.two"],
            ["kopia", "policy", "set", str(tmp_path), "--clear-ignore"],
        ]

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

    def test_prune_real_run_passes_delete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """dry_run=False must actually delete snapshots, not just report them."""
        backend = KopiaBackend({"repository_password": "test-password"})
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
        monkeypatch.setattr(backend, "_connect_repo", lambda _: (True, "connected"))

        def run(command: list[str], **_: object) -> CompletedProcess[str]:
            calls.append(command)
            return CompletedProcess(command, 0, "", "")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        result = backend.prune(BackupDestination("repo"), keep_last=5, dry_run=False)

        assert result.success
        expire_call = next(c for c in calls if c[1:3] == ["snapshot", "expire"])
        assert "--delete" in expire_call
        assert "--dry-run" not in expire_call

    def test_prune_dry_run_omits_delete_and_unsupported_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """dry_run=True must not pass --dry-run (kopia has no such flag) or --delete."""
        backend = KopiaBackend({"repository_password": "test-password"})
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
        monkeypatch.setattr(backend, "_connect_repo", lambda _: (True, "connected"))

        def run(command: list[str], **_: object) -> CompletedProcess[str]:
            calls.append(command)
            return CompletedProcess(command, 0, "", "")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        result = backend.prune(BackupDestination("repo"), keep_last=5, dry_run=True)

        assert result.success
        expire_call = next(c for c in calls if c[1:3] == ["snapshot", "expire"])
        assert "--delete" not in expire_call
        assert "--dry-run" not in expire_call

    def test_prune_refuses_with_no_retention_policy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No keep_* at all must refuse and spawn no kopia process that could delete."""
        backend = KopiaBackend({"repository_password": "test-password"})
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
        monkeypatch.setattr(backend, "_connect_repo", lambda _: (True, "connected"))

        def run(command: list[str], **_: object) -> CompletedProcess[str]:
            calls.append(command)
            return CompletedProcess(command, 0, "", "")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        result = backend.prune(BackupDestination("repo"))

        assert not result.success
        assert "no retention policy" in result.errors[0].lower()
        assert calls == []

    def test_prune_without_source_path_targets_global_policy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No source_path -> repository-wide prune, as before."""
        backend = KopiaBackend({"repository_password": "test-password"})
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
        monkeypatch.setattr(backend, "_connect_repo", lambda _: (True, "connected"))

        def run(command: list[str], **_: object) -> CompletedProcess[str]:
            calls.append(command)
            return CompletedProcess(command, 0, "", "")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        result = backend.prune(BackupDestination("repo"), keep_last=5, dry_run=True)

        assert result.success
        policy_call = next(c for c in calls if c[1:3] == ["policy", "set"])
        assert "--global" in policy_call
        expire_call = next(c for c in calls if c[1:3] == ["snapshot", "expire"])
        assert "--all" in expire_call

    def test_prune_with_source_path_scopes_to_source_and_skips_global(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """source_path -> target that one source, never --global."""
        backend = KopiaBackend({"repository_password": "test-password"})
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
        monkeypatch.setattr(backend, "_connect_repo", lambda _: (True, "connected"))
        snapshot_json = (
            '[{"source": {"host": "myhost", "userName": "myuser", "path": "/data/app"}}]'
        )

        def run(command: list[str], **_: object) -> CompletedProcess[str]:
            calls.append(command)
            if command[1:3] == ["snapshot", "list"]:
                return CompletedProcess(command, 0, snapshot_json, "")
            return CompletedProcess(command, 0, "", "")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        result = backend.prune(BackupDestination("repo"), keep_last=5, dry_run=True, source_path="/data/app")

        assert result.success
        policy_call = next(c for c in calls if c[1:3] == ["policy", "set"])
        assert "--global" not in policy_call
        assert "myuser@myhost:/data/app" in policy_call
        expire_call = next(c for c in calls if c[1:3] == ["snapshot", "expire"])
        assert "myuser@myhost:/data/app" in expire_call
        assert "--all" not in expire_call

    def test_prune_with_unresolvable_source_path_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cannot build a source target -> refuse, never fall back to --global."""
        backend = KopiaBackend({"repository_password": "test-password"})
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
        monkeypatch.setattr(backend, "_connect_repo", lambda _: (True, "connected"))

        def run(command: list[str], **_: object) -> CompletedProcess[str]:
            calls.append(command)
            if command[1:3] == ["snapshot", "list"]:
                return CompletedProcess(command, 0, "[]", "")
            return CompletedProcess(command, 0, "", "")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        result = backend.prune(BackupDestination("repo"), keep_last=5, source_path="/no/such/source")

        assert not result.success
        assert not any(c[1:3] == ["policy", "set"] for c in calls)
        assert not any(c[1:3] == ["snapshot", "expire"] for c in calls)

    def test_prune_keep_yearly_emits_keep_annual_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """keep_yearly maps to kopia's --keep-annual, not --keep-yearly."""
        backend = KopiaBackend({"repository_password": "test-password"})
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
        monkeypatch.setattr(backend, "_connect_repo", lambda _: (True, "connected"))

        def run(command: list[str], **_: object) -> CompletedProcess[str]:
            calls.append(command)
            return CompletedProcess(command, 0, "", "")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        result = backend.prune(BackupDestination("repo"), keep_yearly=3, dry_run=True)

        assert result.success
        policy_call = next(c for c in calls if c[1:3] == ["policy", "set"])
        assert "--keep-annual" in policy_call
        assert "3" in policy_call
        assert "--keep-yearly" not in policy_call

    def test_check_runs_snapshot_verify(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """check() must run a real kopia subcommand, not 'repository validate-client'."""
        backend = KopiaBackend({"repository_password": "test-password"})
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
        monkeypatch.setattr(backend, "_connect_repo", lambda _: (True, "connected"))

        def run(command: list[str], **_: object) -> CompletedProcess[str]:
            calls.append(command)
            return CompletedProcess(command, 0, "", "")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        result = backend.check(BackupDestination("repo"))

        assert result.success
        assert calls[0] == ["kopia", "snapshot", "verify", "--verify-files-percent=0"]

    def test_check_verify_files_percent_is_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Full content verification is opt-in via config, since it is much slower."""
        backend = KopiaBackend({"repository_password": "test-password", "verify_files_percent": 100})
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
        monkeypatch.setattr(backend, "_connect_repo", lambda _: (True, "connected"))

        def run(command: list[str], **_: object) -> CompletedProcess[str]:
            calls.append(command)
            return CompletedProcess(command, 0, "", "")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        backend.check(BackupDestination("repo"))

        assert calls[0] == ["kopia", "snapshot", "verify", "--verify-files-percent=100"]
