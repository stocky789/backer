"""Tests for backup backends."""

import os
import signal
import subprocess
import threading
import time
from datetime import datetime
from io import StringIO
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from backer.backends.base import BackendResult, BackendType, BackupDestination, BackupSource, OperationType
from backer.backends.kopia import (
    KopiaBackend,
    _parse_restore_progress,
    _parse_snapshot_progress,
    _run_kopia_with_progress,
)
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

    def test_proxy_maintenance_operations_do_not_make_requests(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = ProxyBackend({"location": "proxy://backer.example.com/repo/repo-123"})
        monkeypatch.setattr(backend, "_request", lambda *_args, **_kwargs: pytest.fail("unexpected network request"))
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

    def test_snapshot_progress_uses_hashed_and_cached_against_previous_snapshot_size(self) -> None:
        """A recorded Kopia CR frame must report real completed bytes, never an invented percent."""
        event = _parse_snapshot_progress(
            " * 0 hashing, 60 hashed (720 MB), 4 cached (8 MB), uploaded 713.2 MB, estimating...",
            800 * 1024 * 1024,
        )

        assert event == {
            "bytes_done": 728 * 1024 * 1024,
            "total_bytes": 800 * 1024 * 1024,
            "files_done": 64,
            "hashed_bytes": 720 * 1024 * 1024,
            "cached_bytes": 8 * 1024 * 1024,
            "hashed_files": 60,
            "cached_files": 4,
        }

    def test_snapshot_progress_without_previous_snapshot_stays_indeterminate(self) -> None:
        """Removing the prior snapshot size must not manufacture a denominator."""
        event = _parse_snapshot_progress(
            " * 0 hashing, 60 hashed (720 MB), 0 cached (0 B), uploaded 713.2 MB, estimating...", None
        )

        assert event == {
            "bytes_done": 720 * 1024 * 1024,
            "total_bytes": 0,
            "files_done": 60,
            "hashed_bytes": 720 * 1024 * 1024,
            "cached_bytes": 0,
            "hashed_files": 60,
            "cached_files": 0,
        }

    def test_restore_progress_uses_kopias_processed_denominator(self) -> None:
        """Changing the restore frame parser must fail this direct Kopia-output contract."""
        assert _parse_restore_progress("Processed 17 (216 MB) of 60 (720 MB).") == {
            "bytes_done": 216 * 1024 * 1024,
            "total_bytes": 720 * 1024 * 1024,
            "files_done": 17,
            "total_files": 60,
        }

    def test_process_owner_is_single_operation_only(self) -> None:
        """Reusing one owner must never let a later run steal cancellation."""
        from backer.backends.kopia import KopiaProcessOwner

        owner = KopiaProcessOwner()
        first, second = object(), object()
        owner.register(first)

        with pytest.raises(RuntimeError, match="single operation"):
            owner.register(second)

    def test_process_owner_latches_cancel_before_child_registers(self) -> None:
        from backer.backends.kopia import KopiaProcessOwner

        owner = KopiaProcessOwner()
        owner.cancel()

        assert owner.register(object()) is False

    def test_progress_reader_splits_carriage_returns_and_keeps_result_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If stderr CR handling regresses, callbacks disappear while Kopia's JSON result must survive."""
        events: list[dict[str, int]] = []

        class Process:
            stdout = StringIO('{"id":"snapshot"}\n')
            stderr = StringIO(" * 0 hashing, 1 hashed (2 MB), 0 cached (0 B), uploaded 0 B\r")
            returncode = 0

            def wait(self, timeout: int | None = None) -> int:
                return self.returncode

        monkeypatch.setattr("backer.backends.kopia.subprocess.Popen", lambda *_args, **_kwargs: Process())

        result = _run_kopia_with_progress(
            ["kopia", "snapshot", "create", "--json", "--progress", "source"],
            {},
            lambda frame: _parse_snapshot_progress(frame, 4 * 1024 * 1024),
            lambda **event: events.append(event),
            60,
        )

        assert result.stdout == '{"id":"snapshot"}\n'
        assert events == [{
            "bytes_done": 2 * 1024 * 1024,
            "total_bytes": 4 * 1024 * 1024,
            "files_done": 1,
            "hashed_bytes": 2 * 1024 * 1024,
            "cached_bytes": 0,
            "hashed_files": 1,
            "cached_files": 0,
        }]

    def test_progress_runner_interrupts_kopia_before_propagating_ctrl_c(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Removing the interrupt cleanup would leave a connected Kopia process behind."""
        sent: list[int] = []

        class Process:
            stdout = StringIO("")
            stderr = StringIO("")

            def wait(self, timeout: int | None = None) -> int:
                if timeout != 30:
                    raise KeyboardInterrupt
                return 0

            def send_signal(self, value: int) -> None:
                sent.append(value)

        monkeypatch.setattr("backer.backends.kopia.subprocess.Popen", lambda *_args, **_kwargs: Process())

        with pytest.raises(KeyboardInterrupt):
            _run_kopia_with_progress(["kopia"], {}, lambda _: None, None, 60)

        assert sent == [signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT]

    def test_progress_runner_cleans_up_after_timeout_before_reraising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A wait timeout must signal and drain Kopia before the backend reports its normal timeout result."""
        sent: list[int] = []
        waits: list[int | None] = []

        class Process:
            stdout = StringIO("")
            stderr = StringIO("")

            def wait(self, timeout: int | None = None) -> int:
                waits.append(timeout)
                if len(waits) == 1:
                    raise subprocess.TimeoutExpired(["kopia"], timeout)
                return 0

            def send_signal(self, value: int) -> None:
                sent.append(value)

        monkeypatch.setattr("backer.backends.kopia.subprocess.Popen", lambda *_args, **_kwargs: Process())

        with pytest.raises(subprocess.TimeoutExpired):
            _run_kopia_with_progress(["kopia"], {}, lambda _: None, None, 1)

        assert sent == [signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT]
        assert waits == [1, 30]

    def test_progress_runner_bounds_a_hard_kill_that_never_reaps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unkillable child must not leave the timeout path waiting forever on pipes or wait()."""
        waits: list[int | None] = []
        killed = []

        class Process:
            stdout = StringIO("")
            stderr = StringIO("")

            def wait(self, timeout: int | None = None) -> int:
                waits.append(timeout)
                raise subprocess.TimeoutExpired(["kopia"], timeout)

            def send_signal(self, _value: int) -> None:
                pass

            def kill(self) -> None:
                killed.append(True)

        monkeypatch.setattr("backer.backends.kopia.subprocess.Popen", lambda *_args, **_kwargs: Process())

        with pytest.raises(subprocess.TimeoutExpired):
            _run_kopia_with_progress(["kopia"], {}, lambda _: None, None, 1)

        assert killed == [True]
        assert waits == [1, 30, 5]

    def test_connect_requires_repository_password_before_kopia_runs(self) -> None:
        success, message = KopiaBackend()._connect_repo("/backup/repo")
        assert not success
        assert message == "Repository encryption password is required"

    def test_backup_replaces_kopia_ignore_policy_for_changed_or_removed_excludes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        backend = KopiaBackend({"repository_password": "test-password"})
        calls: list[list[str]] = []
        progress_calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
        monkeypatch.setattr(backend, "_connect_repo", lambda _: (True, "connected"))

        def run(command: list[str], **_: object) -> CompletedProcess[str]:
            calls.append(command)
            return CompletedProcess(command, 0, '{"id":"snapshot"}\n', "")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        def run_progress(command: list[str], *_: object) -> CompletedProcess[str]:
            progress_calls.append(command)
            return CompletedProcess(command, 0, '{"id":"snapshot"}\n', "")

        monkeypatch.setattr("backer.backends.kopia._run_kopia_with_progress", run_progress)
        for excludes in (["*.one"], ["*.two"], []):
            assert backend.backup(BackupSource(tmp_path, excludes=excludes), BackupDestination("repo")).success

        policies = [call for call in calls if call[1:3] == ["policy", "set"]]
        assert policies == [
            ["kopia", "policy", "set", str(tmp_path), "--clear-ignore", "--add-ignore", "*.one"],
            ["kopia", "policy", "set", str(tmp_path), "--clear-ignore", "--add-ignore", "*.two"],
            ["kopia", "policy", "set", str(tmp_path), "--clear-ignore"],
        ]
        assert progress_calls == [
            ["kopia", "snapshot", "create", "--json", "--progress", str(tmp_path)],
            ["kopia", "snapshot", "create", "--json", "--progress", str(tmp_path)],
            ["kopia", "snapshot", "create", "--json", "--progress", str(tmp_path)],
        ]

    def test_restore_dry_run_is_rejected_without_running_kopia(self) -> None:
        result = KopiaBackend().restore(BackupDestination(path="/backup"), Path("/restore"), dry_run=True)

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
        backend = KopiaBackend(
            {
                "s3": {
                    "bucket": "mybucket",
                    "prefix": "prefix",
                    "endpoint": "https://minio.test",
                    "region": "us-east-1",
                    "access_key_id": "access",
                    "secret_access_key": "secret",
                }
            }
        )
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
        result = backend.prune(BackupDestination("repo"), keep_last=5, dry_run=False)

        assert result.success
        policy_call = next(c for c in calls if c[1:3] == ["policy", "set"])
        assert "--global" in policy_call
        expire_call = next(c for c in calls if c[1:3] == ["snapshot", "expire"])
        assert "--all" in expire_call

    def test_prune_preview_writes_policy_then_omits_delete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The preview uses the proposed per-source policy and never deletes."""
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
        assert any(c[1:3] == ["policy", "set"] for c in calls)
        expire_call = next(c for c in calls if c[1:3] == ["snapshot", "expire"])
        assert "--all" in expire_call
        assert "--delete" not in expire_call

    def test_prune_with_source_path_scopes_to_source_and_skips_global(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """source_path -> target that one source, never --global."""
        backend = KopiaBackend({"repository_password": "test-password"})
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
        monkeypatch.setattr(backend, "_connect_repo", lambda _: (True, "connected"))
        snapshot_json = '[{"source": {"host": "myhost", "userName": "myuser", "path": "/data/app"}}]'

        def run(command: list[str], **_: object) -> CompletedProcess[str]:
            calls.append(command)
            if command[1:3] == ["snapshot", "list"]:
                return CompletedProcess(command, 0, snapshot_json, "")
            return CompletedProcess(command, 0, "", "")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        result = backend.prune(BackupDestination("repo"), keep_last=5, dry_run=False, source_path="/data/app")

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
        result = backend.prune(BackupDestination("repo"), keep_yearly=3, dry_run=False)

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

    def test_repo_env_isolates_config_and_cache_per_repository(self) -> None:
        """Two repositories must never share a kopia config file or cache dir."""
        backend = KopiaBackend({"repository_password": "test-password"})
        env_a = backend._repo_env("/backup/repoA")
        env_b = backend._repo_env("/backup/repoB")

        assert env_a["KOPIA_CONFIG_PATH"] != env_b["KOPIA_CONFIG_PATH"]
        assert env_a["KOPIA_CACHE_DIRECTORY"] != env_b["KOPIA_CACHE_DIRECTORY"]
        # Same repo path -> same config every time, so a later disconnect for
        # that repo always lands on that repo's own connection state.
        assert backend._repo_env("/backup/repoA")["KOPIA_CONFIG_PATH"] == env_a["KOPIA_CONFIG_PATH"]

    def test_disconnect_cannot_touch_a_different_repositorys_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One operation's disconnect must not be able to tear down a sibling repo's connection."""
        backend = KopiaBackend({"repository_password": "test-password"})
        calls: list[tuple[list[str], dict[str, str]]] = []
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))

        def run(command: list[str], **kwargs: object) -> CompletedProcess[str]:
            calls.append((command, kwargs["env"]))
            return CompletedProcess(command, 0, "", "")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        backend._disconnect_repo("/backup/repoA")
        backend._disconnect_repo("/backup/repoB")

        assert calls[0][1]["KOPIA_CONFIG_PATH"] != calls[1][1]["KOPIA_CONFIG_PATH"]

    def test_backup_connect_failure_that_is_not_absent_does_not_init(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An unreachable/unmounted destination must fail closed, not auto-create a repo there."""
        backend = KopiaBackend({"repository_password": "test-password"})
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))

        def run(command: list[str], **_: object) -> CompletedProcess[str]:
            calls.append(command)
            if command[1:3] == ["repository", "connect"]:
                return CompletedProcess(command, 1, "", "can't connect to storage: cannot access storage path")
            return CompletedProcess(command, 0, "", "")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        result = backend.backup(BackupSource(tmp_path), BackupDestination("repo"))

        assert not result.success
        assert not any(c[1:3] == ["repository", "create"] for c in calls)
        assert "can't connect to storage" in result.errors[0]

    def test_backup_wrong_password_reports_as_such_not_as_init_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A wrong passphrase against a real repository must be reported as such, never auto-init."""
        backend = KopiaBackend({"repository_password": "wrong-password"})
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))

        def run(command: list[str], **_: object) -> CompletedProcess[str]:
            calls.append(command)
            if command[1:3] == ["repository", "connect"]:
                return CompletedProcess(command, 1, "", "unable to create format manager: invalid repository password")
            return CompletedProcess(command, 0, "", "")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        result = backend.backup(BackupSource(tmp_path), BackupDestination("repo"))

        assert not result.success
        assert not any(c[1:3] == ["repository", "create"] for c in calls)
        assert "wrong repository password" in result.errors[0].lower()

    def test_backup_refuses_an_uninitialized_repository_without_creating_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Only explicit repo add --init may create storage."""
        backend = KopiaBackend({"repository_password": "test-password"})
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))

        def run(command: list[str], **_: object) -> CompletedProcess[str]:
            calls.append(command)
            if command[1:3] == ["repository", "connect"]:
                return CompletedProcess(
                    command, 1, "", "error connecting to repository: repository not initialized in the provided storage"
                )
            if command[1:3] == ["repository", "create"]:
                return CompletedProcess(command, 0, "Connected to repository.", "")
            return CompletedProcess(command, 0, '{"id":"snapshot"}\n', "")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        result = backend.backup(BackupSource(tmp_path), BackupDestination(str(tmp_path / "repo")))

        assert not result.success
        assert not any(c[1:3] == ["repository", "create"] for c in calls)
        assert "repository must already be initialized" in result.errors[0].lower()

    def test_hard_killed_backup_names_local_unlock_and_isolated_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        backend = KopiaBackend({"repository_password": "test-password"})
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
        monkeypatch.setattr(backend, "_connect_repo", lambda _path: (True, ""))
        monkeypatch.setattr(
            "backer.backends.kopia.subprocess.run", lambda command, **_kwargs: CompletedProcess(command, 0, "", "")
        )
        error = subprocess.TimeoutExpired(["kopia"], 1)
        error.backer_hard_stopped = True
        monkeypatch.setattr(
            "backer.backends.kopia._run_kopia_with_progress", lambda *_args: (_ for _ in ()).throw(error)
        )

        result = backend.backup(BackupSource(tmp_path), BackupDestination("repository"))

        assert not result.success
        assert "backer repo unlock NAME" in result.errors[0]
        assert "KOPIA_CONFIG_PATH" not in result.errors[0]
        assert "repository.config" in result.errors[0]

    def test_hard_killed_restore_names_local_unlock_and_isolated_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        backend = KopiaBackend({"repository_password": "test-password"})
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
        monkeypatch.setattr(backend, "_connect_repo", lambda _path: (True, ""))
        error = subprocess.TimeoutExpired(["kopia"], 1)
        error.backer_hard_stopped = True
        monkeypatch.setattr(
            "backer.backends.kopia._run_kopia_with_progress", lambda *_args: (_ for _ in ()).throw(error)
        )

        result = backend.restore(BackupDestination("repository"), tmp_path, snapshot="snapshot")

        assert not result.success
        assert "backer repo unlock NAME" in result.errors[0]
        assert "repository.config" in result.errors[0]

    def test_backup_does_not_auto_init_into_a_nonempty_existing_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A 'not initialized' path that already resolves to a non-empty local directory must not be created into."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "unrelated.txt").write_text("not a kopia repo")

        backend = KopiaBackend({"repository_password": "test-password"})
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))

        def run(command: list[str], **_: object) -> CompletedProcess[str]:
            calls.append(command)
            if command[1:3] == ["repository", "connect"]:
                return CompletedProcess(
                    command, 1, "", "error connecting to repository: repository not initialized in the provided storage"
                )
            return CompletedProcess(command, 0, "", "")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        result = backend.backup(BackupSource(tmp_path), BackupDestination(str(repo_dir)))

        assert not result.success
        assert not any(c[1:3] == ["repository", "create"] for c in calls)

    def test_repo_lock_serializes_operations_on_the_same_repository(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Two operations against the same repository must never run their critical sections concurrently."""
        monkeypatch.setattr("backer.core.paths.get_data_dir", lambda: tmp_path)
        backend = KopiaBackend()
        order: list[str] = []

        def worker(label: str, hold: float) -> None:
            with backend._repo_lock("/backup/shared-repo"):
                order.append(f"{label}-start")
                time.sleep(hold)
                order.append(f"{label}-end")

        t1 = threading.Thread(target=worker, args=("first", 0.15))
        t2 = threading.Thread(target=worker, args=("second", 0.0))
        t1.start()
        time.sleep(0.05)
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert order == ["first-start", "first-end", "second-start", "second-end"]

    def test_repo_lock_does_not_block_across_different_repositories(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Two operations against different repositories must be able to run concurrently."""
        monkeypatch.setattr("backer.core.paths.get_data_dir", lambda: tmp_path)
        backend = KopiaBackend()
        barrier = threading.Barrier(2, timeout=5)
        entered: list[str] = []

        def worker(label: str, repo_path: str) -> None:
            with backend._repo_lock(repo_path):
                entered.append(label)
                barrier.wait()  # only reachable by both if neither is blocked on the other's lock

        t1 = threading.Thread(target=worker, args=("A", "/backup/repoA"))
        t2 = threading.Thread(target=worker, args=("B", "/backup/repoB"))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert set(entered) == {"A", "B"}

    def test_find_latest_snapshot_refuses_basename_only_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A snapshot from an unrelated directory that merely shares a basename must never be selected."""
        backend = KopiaBackend({"repository_password": "test-password"})
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
        snapshot_json = (
            '[{"id": "wrong-repo-snapshot", "source": {"path": "D:/Archive/Documents"}, '
            '"startTime": "2024-01-01T00:00:00Z"}]'
        )

        def run(command: list[str], **_: object) -> CompletedProcess[str]:
            return CompletedProcess(command, 0, snapshot_json, "")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        result = backend._find_latest_snapshot_for_source("repo", "C:/Users/alice/Documents")

        assert result is None

    def test_get_snapshot_files_passes_a_source_where_a_source_is_expected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'snapshot list' takes a source path, not a snapshot id, and has no --path flag.

        Browsing a snapshot's contents is 'kopia ls <snapshot-id>', since a
        snapshot id is itself a valid object path at the snapshot root.
        """
        backend = KopiaBackend({"repository_password": "test-password"})
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
        monkeypatch.setattr(backend, "_connect_repo", lambda _: (True, "connected"))

        ls_output = (
            "drwxrwxrwx            6 2026-08-31 23:48:18 AEST k090c70a25a6eac07a41461bbfe109552  sub/\n"
            "-rw-rw-rw-            6 2026-08-31 23:48:18 AEST f218bb89b4c096463f45e07b2ef3a5ef   a.txt\n"
        )

        def run(command: list[str], **_: object) -> CompletedProcess[str]:
            calls.append(command)
            return CompletedProcess(command, 0, ls_output, "")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        entries = backend.get_snapshot_files(BackupDestination("repo"), "f0a62d5b3dc5a02eb2674791653ebb78")

        assert calls[0] == ["kopia", "ls", "--long", "--show-object-id", "f0a62d5b3dc5a02eb2674791653ebb78"]
        assert not any("--path" in c for c in calls)
        assert not any(c[1:3] == ["snapshot", "list"] for c in calls)
        assert {
            "name": "sub",
            "type": "dir",
            "size": 6,
            "mtime": "2026-08-31 23:48:18 AEST",
            "object_id": "k090c70a25a6eac07a41461bbfe109552",
        } in entries
        assert {
            "name": "a.txt",
            "type": "file",
            "size": 6,
            "mtime": "2026-08-31 23:48:18 AEST",
            "object_id": "f218bb89b4c096463f45e07b2ef3a5ef",
        } in entries

    def test_get_snapshot_files_lists_a_subdirectory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A path argument addresses '<snapshot-id>/<path>', a subdirectory within the snapshot."""
        backend = KopiaBackend({"repository_password": "test-password"})
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
        monkeypatch.setattr(backend, "_connect_repo", lambda _: (True, "connected"))

        ls_output = "-rw-rw-rw-            2 2026-08-31 23:48:59 AEST 0a710ee49acdd7a9478fca18433356a9   c file.txt\n"

        def run(command: list[str], **_: object) -> CompletedProcess[str]:
            calls.append(command)
            return CompletedProcess(command, 0, ls_output, "")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        entries = backend.get_snapshot_files(
            BackupDestination("repo"), "af85cb18d4d83b05392e2ffe18472b74", path="sub with space"
        )

        assert calls[0][-1] == "af85cb18d4d83b05392e2ffe18472b74/sub with space"
        assert entries == [
            {
                "name": "c file.txt",
                "type": "file",
                "size": 2,
                "mtime": "2026-08-31 23:48:59 AEST",
                "object_id": "0a710ee49acdd7a9478fca18433356a9",
            }
        ]

    def test_get_snapshot_files_fails_closed_on_bad_snapshot_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A kopia error (nonexistent snapshot/path) must return no files, not raise or fabricate a listing."""
        backend = KopiaBackend({"repository_password": "test-password"})
        monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
        monkeypatch.setattr(backend, "_connect_repo", lambda _: (True, "connected"))

        def run(command: list[str], **_: object) -> CompletedProcess[str]:
            return CompletedProcess(command, 1, "", "unable to get filesystem directory entry: not a directory object")

        monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
        entries = backend.get_snapshot_files(BackupDestination("repo"), "deadbeef")

        assert entries == []
