import pathlib
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backer.agent.service import AgentService
from backer.backends.base import BackendResult, OperationType
from backer.backends.kopia import KopiaBackend
from backer.backends.proxy import ProxyBackend
from backer.client import agent as client_agent
from backer.client.agent import BackerAgent, _backend_for_location
from backer.core.repo_metadata import RepositoryMetadata


class _RestoreBackend:
    def __init__(
        self,
        validation_success: bool,
        restore_success: bool = True,
        restore_files: int | None = None,
        restore_dirs_only: bool = False,
        resolved_snapshot: str = "a" * 64,
    ):
        self.validation_success = validation_success
        self.restore_success = restore_success
        # Number of files the (non-dry-run) restore actually writes to the
        # destination. None means "write one", 0 simulates a restore that
        # reports success but matches/restores nothing.
        self.restore_files = restore_files
        # Simulates a restore (or include_path-scoped subtree) that contains
        # only directories: Kopia reports success and writes the directory
        # tree but no files.
        self.restore_dirs_only = restore_dirs_only
        self.resolved_snapshot = resolved_snapshot
        self.dry_runs: list[bool] = []
        self.snapshots: list[str | None] = []
        self.resolver_calls: list[str] = []

    def check_available(self) -> tuple[bool, str]:
        return True, "ready"

    def resolve_latest_snapshot(self, destination: object) -> str:
        self.resolver_calls.append(str(getattr(destination, "path")))
        return self.resolved_snapshot

    def list_snapshots(self, destination: object) -> list[dict[str, str]]:
        # Kopia validates a clean restore by confirming the repository opens
        # and the selected snapshot exists (it refuses dry-run restores), so
        # an unavailable/unvalidatable repository is modeled as an empty list.
        if not self.validation_success:
            return []
        return [{"id": self.resolved_snapshot, "full_id": self.resolved_snapshot}, {"id": "chosen"}]

    def restore(self, *, destination: Path, dry_run: bool, **kwargs: object) -> BackendResult:
        self.dry_runs.append(dry_run)
        self.snapshots.append(kwargs.get("snapshot") if isinstance(kwargs.get("snapshot"), str) else None)
        if not self.restore_success:
            (destination / "partial.txt").write_text("partial")
            files_written = 0
        elif not dry_run:
            if self.restore_dirs_only:
                (destination / "empty_subdir").mkdir(parents=True, exist_ok=True)
                files_written = 0
            else:
                files_written = self.restore_files if self.restore_files is not None else 1
                for i in range(files_written):
                    (destination / f"restored{i}.txt").write_text("restored")
        else:
            files_written = 0
        return BackendResult(
            success=self.restore_success,
            operation=OperationType.RESTORE,
            started_at=datetime.now(),
            finished_at=datetime.now(),
            errors=[] if self.restore_success else ["repository unavailable"],
            files_transferred=files_written,
        )


def _agent(tmp_path: Path) -> BackerAgent:
    return BackerAgent("http://example.test", "agent", "secret", tmp_path / "agent.yaml")


def test_agent_routes_direct_locations_to_kopia() -> None:
    assert isinstance(_backend_for_location("//nas/backups/photos", {"repository_password": "secret"}), KopiaBackend)


def test_agent_routes_proxy_locations_to_proxy() -> None:
    assert isinstance(_backend_for_location("proxys://backer.example/repo/repo-1", {}), ProxyBackend)


def test_agent_metadata_does_not_write_backend(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    agent._write_metadata_to_path(
        tmp_path / "repository",
        "photos",
        "run-1",
        "/photos",
        "kopia",
        SimpleNamespace(success=True, bytes_transferred=1, files_transferred=1, errors=[]),
        datetime.now(),
        datetime.now(),
        "snapshot-1",
    )

    metadata = RepositoryMetadata(tmp_path / "repository")
    assert "backend" not in metadata.get_job("photos")["config"]
    assert "backend" not in metadata.get_snapshot("snapshot-1")


def test_background_command_acknowledges_only_after_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _agent(tmp_path)
    started = threading.Event()
    finish = threading.Event()
    acknowledged_event = threading.Event()
    acknowledged: list[int] = []

    def run_backup(*_: object, **__: object) -> None:
        started.set()
        finish.wait(timeout=1)

    def acknowledge(command_id: int) -> None:
        acknowledged.append(command_id)
        acknowledged_event.set()

    monkeypatch.setattr(agent, "execute_backup", run_backup)
    monkeypatch.setattr(agent, "_acknowledge_command", acknowledge)
    command = {"id": 7, "command_type": "backup", "payload": {"job_name": "photos"}}

    agent._handle_command(command)
    assert started.wait(timeout=1)
    agent._handle_command(command)
    assert acknowledged == []
    finish.set()
    assert acknowledged_event.wait(timeout=1)
    assert acknowledged == [7]


def test_android_acknowledges_terminal_commands_without_interrupting_proxy_workers() -> None:
    heartbeat = Path("android/app/src/main/java/com/backer/android/worker/HeartbeatWorker.kt").read_text()
    handler = Path("android/app/src/main/java/com/backer/android/worker/CommandHandler.kt").read_text()

    assert 'command.commandType !in setOf("backup", "restore")' in heartbeat
    assert handler.count("ExistingWorkPolicy.KEEP") == 2
    assert handler.count("apiRepository.acknowledgeCommand(command.id)") == 2
    assert "reportMissingProxyCapability(runId, \"backup\", command.id)" in handler
    assert "reportMissingProxyCapability(runId, \"restore\", command.id)" in handler


def test_windows_nfs_is_rejected_before_mounting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _agent(tmp_path)
    monkeypatch.setattr(client_agent.sys, "platform", "win32")

    with pytest.raises(RuntimeError, match="NFS destinations are not supported"):
        agent._prepare_destination_for_backend(
            {"destination_path": "nas:/exports/backups"}, "kopia"
        )

    with pytest.raises(RuntimeError, match="NFS restores are not supported"):
        agent._prepare_source_for_backend(
            {"source_path": "nas:/exports/backups"}, "kopia"
        )


def test_windows_smb_is_prepared_for_backup_and_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _agent(tmp_path)
    connections: list[tuple[str, str]] = []

    class SMBConnectionManager:
        def connect(self, server: str, share: str, *_: object) -> bool:
            connections.append((server, share))
            return True

    monkeypatch.setattr(client_agent.sys, "platform", "win32")
    from backer.agent import service as gui_service
    monkeypatch.setattr(gui_service, "SMBConnectionManager", SMBConnectionManager)
    job = {"smb_username": "user", "smb_password": "secret"}

    agent._prepare_destination_for_backend({**job, "destination_path": "//nas/backups/repo"}, "kopia")
    agent._prepare_source_for_backend({**job, "source_path": "//nas/backups/repo"}, "kopia")

    assert connections == [("nas", "backups"), ("nas", "backups")]


def test_windows_smb_preparation_reuses_process_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """Creating a manager per command reintroduces Windows error 1219 conflicts."""
    from backer.core import destination

    managers: list[object] = []

    class SMBConnectionManager:
        def __init__(self) -> None:
            managers.append(self)

        def connect(self, *_: object) -> bool:
            return True

    monkeypatch.setattr(destination.sys, "platform", "win32")
    monkeypatch.setattr(destination, "_smb_manager", None)
    from backer.agent import service as gui_service

    monkeypatch.setattr(gui_service, "SMBConnectionManager", SMBConnectionManager)
    job = {"smb_username": "user", "smb_password": "secret"}

    destination.prepare_destination({**job, "destination_path": "//nas/backups/repo"}, "kopia")
    destination.prepare_source({**job, "source_path": "//nas/backups/repo"}, "kopia")

    assert len(managers) == 1


def test_windows_smb_connection_failure_keeps_unc_error_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed UNC connection must identify the share with its full UNC path."""
    from backer.core import destination

    class SMBConnectionManager:
        def connect(self, *_: object) -> bool:
            return False

    monkeypatch.setattr(destination, "_smb_manager", SMBConnectionManager())

    with pytest.raises(RuntimeError, match=r"^Failed to connect to SMB share: \\\\nas\\backups$"):
        destination._prepare_windows_smb("//nas/backups/repo", {})


def test_agent_preparation_uses_mount_check_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent mount availability hooks remain effective after core extraction."""
    from backer.core import destination

    agent = _agent(tmp_path)
    checks: list[str] = []
    monkeypatch.setattr(destination.sys, "platform", "linux")
    monkeypatch.setattr(agent, "_check_cifs_available", lambda: checks.append("cifs") or False)
    monkeypatch.setattr(agent, "_check_nfs_available", lambda: checks.append("nfs") or False)

    with pytest.raises(RuntimeError, match="cifs-utils not installed"):
        agent._prepare_destination_for_backend({"destination_path": "//nas/backups"}, "kopia")
    with pytest.raises(RuntimeError, match="NFS mount tools not installed"):
        agent._prepare_source_for_backend({"source_path": "nas:/backups"}, "kopia")

    assert checks == ["cifs", "nfs"]


def test_linux_smb_password_uses_private_credentials_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _agent(tmp_path)
    commands: list[list[str]] = []
    credential_contents: list[str] = []

    monkeypatch.setattr(agent, "_check_cifs_available", lambda: True)

    def run(cmd: list[str], **_: object) -> MagicMock:
        commands.append(cmd)
        if cmd[:3] == ["mount", "-t", "cifs"]:
            options = cmd[cmd.index("-o") + 1].split(",")
            credential_path = Path(
                next(option.split("=", 1)[1] for option in options if option.startswith("credentials="))
            )
            credential_contents.append(credential_path.read_text(encoding="utf-8"))
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(client_agent.subprocess, "run", run)
    context = agent._smb_mount_context("nas", "backups", "user", "secret", "domain")
    context.__enter__()
    context.__exit__(None, None, None)

    assert "secret" not in " ".join(commands[0])
    assert credential_contents == ["username=user\npassword=secret\ndomain=domain\n"]


def test_clean_restore_keeps_destination_when_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "restore"
    destination.mkdir()
    original = destination / "keep.txt"
    original.write_text("keep")
    backend = _RestoreBackend(validation_success=False)
    agent = _agent(tmp_path)
    monkeypatch.setattr("backer.core.runner.get_backend", lambda *_: backend)
    monkeypatch.setattr(agent, "_report_progress", lambda **_: None)
    monkeypatch.setattr(agent, "_get_client", lambda: type("Client", (), {"post": lambda *_a, **_k: None})())

    report = agent.execute_restore({
        "run_id": "run-1",
        "job_name": "job",
        "backend": "kopia",
        "source_path": str(tmp_path / "repo"),
        "destination_path": str(destination),
        "clean_restore": True,
    })

    assert not report["success"]
    assert original.exists()
    assert original.read_text() == "keep"
    assert backend.dry_runs == []


@pytest.mark.parametrize(
    ("requested_snapshot", "expected_snapshot", "resolver_calls"),
    [(None, "a" * 64, 1), ("latest", "a" * 64, 1), ("chosen", "chosen", 0)],
)
def test_clean_kopia_restore_uses_one_immutable_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested_snapshot: str | None,
    expected_snapshot: str,
    resolver_calls: int,
) -> None:
    destination = tmp_path / "restore"
    backend = _RestoreBackend(validation_success=True)
    agent = _agent(tmp_path)
    monkeypatch.setattr("backer.core.runner.get_backend", lambda *_: backend)
    monkeypatch.setattr(agent, "_report_progress", lambda **_: None)
    monkeypatch.setattr(agent, "_get_client", lambda: type("Client", (), {"post": lambda *_a, **_k: None})())

    report = agent.execute_restore({
        "run_id": "run-1",
        "job_name": "job",
        "backend": "kopia",
        "source_path": str(tmp_path / "repo"),
        "destination_path": str(destination),
        "snapshot": requested_snapshot,
        "clean_restore": True,
    })

    assert report["success"]
    assert len(backend.resolver_calls) == 0
    assert backend.snapshots == [requested_snapshot]


def test_clean_kopia_restore_keeps_destination_when_no_files_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "restore"
    destination.mkdir()
    original = destination / "keep.txt"
    original.write_text("keep")
    backend = _RestoreBackend(validation_success=True, restore_files=0)
    agent = _agent(tmp_path)
    monkeypatch.setattr("backer.core.runner.get_backend", lambda *_: backend)
    monkeypatch.setattr(agent, "_report_progress", lambda **_: None)
    monkeypatch.setattr(agent, "_get_client", lambda: type("Client", (), {"post": lambda *_a, **_k: None})())

    report = agent.execute_restore({
        "run_id": "run-1",
        "job_name": "job",
        "backend": "kopia",
        "source_path": str(tmp_path / "repo"),
        "destination_path": str(destination),
        "clean_restore": True,
    })

    assert not report["success"]
    assert original.exists()
    assert original.read_text() == "keep"
    assert backend.dry_runs == [False]


def test_clean_kopia_restore_rolls_back_when_actual_restore_matches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Validation (repository opens, snapshot exists) passes, but the real,
    # non-dry-run restore call itself writes nothing to the destination -
    # this is the case validation alone cannot catch, since Kopia refuses
    # dry-run restores.
    destination = tmp_path / "restore"
    destination.mkdir()
    original = destination / "keep.txt"
    original.write_text("keep")
    backend = _RestoreBackend(validation_success=True, restore_files=0)
    agent = _agent(tmp_path)
    monkeypatch.setattr("backer.core.runner.get_backend", lambda *_: backend)
    monkeypatch.setattr(agent, "_report_progress", lambda **_: None)
    monkeypatch.setattr(agent, "_get_client", lambda: type("Client", (), {"post": lambda *_a, **_k: None})())

    report = agent.execute_restore({
        "run_id": "run-1",
        "job_name": "job",
        "backend": "kopia",
        "source_path": str(tmp_path / "repo"),
        "destination_path": str(destination),
        "snapshot": "chosen",
        "clean_restore": True,
    })

    assert not report["success"]
    assert original.exists()
    assert original.read_text() == "keep"
    assert backend.dry_runs == [False]


def test_clean_kopia_restore_rolls_back_when_only_directories_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A snapshot (or include_path-scoped subtree) containing only directories
    # restores with exit code 0 and creates the directory tree but no files -
    # any(destination.iterdir()) would see that as "non-empty" and let the
    # staged original be discarded. Directories alone must not count as
    # restored content.
    destination = tmp_path / "restore"
    destination.mkdir()
    original = destination / "keep.txt"
    original.write_text("keep")
    backend = _RestoreBackend(validation_success=True, restore_dirs_only=True)
    agent = _agent(tmp_path)
    monkeypatch.setattr("backer.core.runner.get_backend", lambda *_: backend)
    monkeypatch.setattr(agent, "_report_progress", lambda **_: None)
    monkeypatch.setattr(agent, "_get_client", lambda: type("Client", (), {"post": lambda *_a, **_k: None})())

    report = agent.execute_restore({
        "run_id": "run-1",
        "job_name": "job",
        "backend": "kopia",
        "source_path": str(tmp_path / "repo"),
        "destination_path": str(destination),
        "clean_restore": True,
    })

    assert not report["success"]
    assert original.exists()
    assert original.read_text() == "keep"


def test_clean_kopia_restore_reports_failure_into_new_destination_that_restores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Disaster-recovery case: the destination does not exist yet, so nothing
    # is staged. A restore that matches nothing must still be reported as a
    # failure rather than a green run over an empty directory.
    destination = tmp_path / "restore"
    assert not destination.exists()
    backend = _RestoreBackend(validation_success=True, restore_files=0)
    agent = _agent(tmp_path)
    monkeypatch.setattr("backer.core.runner.get_backend", lambda *_: backend)
    monkeypatch.setattr(agent, "_report_progress", lambda **_: None)
    monkeypatch.setattr(agent, "_get_client", lambda: type("Client", (), {"post": lambda *_a, **_k: None})())

    report = agent.execute_restore({
        "run_id": "run-1",
        "job_name": "job",
        "backend": "kopia",
        "source_path": str(tmp_path / "repo"),
        "destination_path": str(destination),
        "clean_restore": True,
    })

    assert not report["success"]
    assert any("no files" in error.lower() for error in report["errors"])


def test_clean_restore_validates_before_clearing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "restore"
    destination.mkdir()
    destination.chmod(0o750)
    (destination / "old.txt").write_text("old")
    backend = _RestoreBackend(validation_success=True)
    agent = _agent(tmp_path)
    monkeypatch.setattr("backer.core.runner.get_backend", lambda *_: backend)
    monkeypatch.setattr(agent, "_report_progress", lambda **_: None)
    monkeypatch.setattr(agent, "_get_client", lambda: type("Client", (), {"post": lambda *_a, **_k: None})())

    report = agent.execute_restore({
        "run_id": "run-1",
        "job_name": "job",
        "backend": "kopia",
        "source_path": str(tmp_path / "repo"),
        "destination_path": str(destination),
        "clean_restore": True,
    })

    assert report["success"]
    assert backend.dry_runs == [False]
    assert not (destination / "old.txt").exists()
    if __import__("sys").platform != "win32":
        assert destination.stat().st_mode & 0o7777 == 0o750


def test_clean_proxy_restore_keeps_destination_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "restore"
    destination.mkdir()
    original = destination / "keep.txt"
    original.write_text("keep")
    backend = _RestoreBackend(validation_success=True)
    agent = _agent(tmp_path)
    monkeypatch.setattr("backer.core.runner.get_backend", lambda *_: backend)
    monkeypatch.setattr(agent, "_report_progress", lambda **_: None)
    monkeypatch.setattr(agent, "_get_client", lambda: type("Client", (), {"post": lambda *_a, **_k: None})())

    report = agent.execute_restore({
        "run_id": "run-1",
        "job_name": "job",
        "backend": "proxy",
        "source_path": "proxy://example.test/repo/repo-1",
        "destination_path": str(destination),
        "clean_restore": True,
    })

    assert not report["success"]
    assert original.exists()
    assert backend.dry_runs == []


def test_clean_restore_restores_destination_after_preparation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "restore"
    destination.mkdir()
    original = destination / "keep.txt"
    original.write_text("keep")
    backend = _RestoreBackend(validation_success=True)
    agent = _agent(tmp_path)
    monkeypatch.setattr("backer.core.runner.get_backend", lambda *_: backend)
    monkeypatch.setattr(agent, "_report_progress", lambda **_: None)
    monkeypatch.setattr(agent, "_get_client", lambda: type("Client", (), {"post": lambda *_a, **_k: None})())

    original_mkdir = Path.mkdir

    def fail_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path == destination:
            raise PermissionError("denied")
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)

    report = agent.execute_restore({
        "run_id": "run-1",
        "job_name": "job",
        "backend": "kopia",
        "source_path": str(tmp_path / "repo"),
        "destination_path": str(destination),
        "clean_restore": True,
    })

    assert not report["success"]
    assert original.exists()
    assert backend.dry_runs == []


def test_clean_restore_restores_destination_after_backend_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "restore"
    destination.mkdir()
    original = destination / "keep.txt"
    original.write_text("keep")
    backend = _RestoreBackend(validation_success=True, restore_success=False)
    agent = _agent(tmp_path)
    monkeypatch.setattr("backer.core.runner.get_backend", lambda *_: backend)
    monkeypatch.setattr(agent, "_report_progress", lambda **_: None)
    monkeypatch.setattr(agent, "_get_client", lambda: type("Client", (), {"post": lambda *_a, **_k: None})())

    report = agent.execute_restore({
        "run_id": "run-1",
        "job_name": "job",
        "backend": "kopia",
        "source_path": str(tmp_path / "repo"),
        "destination_path": str(destination),
        "clean_restore": True,
    })

    assert not report["success"]
    assert original.read_text() == "keep"
    assert not (destination / "partial.txt").exists()
    assert backend.dry_runs == [False]


def test_clean_restore_removes_partial_new_destination_after_backend_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "restore"
    backend = _RestoreBackend(validation_success=True, restore_success=False)
    agent = _agent(tmp_path)
    monkeypatch.setattr("backer.core.runner.get_backend", lambda *_: backend)
    monkeypatch.setattr(agent, "_report_progress", lambda **_: None)
    monkeypatch.setattr(agent, "_get_client", lambda: type("Client", (), {"post": lambda *_a, **_k: None})())

    report = agent.execute_restore({
        "run_id": "run-1",
        "job_name": "job",
        "backend": "kopia",
        "source_path": str(tmp_path / "repo"),
        "destination_path": str(destination),
        "clean_restore": True,
    })

    assert not report["success"]
    assert not destination.exists()
    assert backend.dry_runs == [False]


def test_clean_restore_reports_rollback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "restore"
    destination.mkdir()
    (destination / "keep.txt").write_text("keep")
    backend = _RestoreBackend(validation_success=True, restore_success=False)
    agent = _agent(tmp_path)
    monkeypatch.setattr("backer.core.runner.get_backend", lambda *_: backend)
    monkeypatch.setattr(agent, "_report_progress", lambda **_: None)
    monkeypatch.setattr(agent, "_get_client", lambda: type("Client", (), {"post": lambda *_a, **_k: None})())
    original_rmtree = client_agent.shutil.rmtree

    def fail_partial_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        if path == destination:
            raise PermissionError("denied")
        original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(client_agent.shutil, "rmtree", fail_partial_cleanup)
    report = agent.execute_restore({
        "run_id": "run-1",
        "job_name": "job",
        "backend": "kopia",
        "source_path": str(tmp_path / "repo"),
        "destination_path": str(destination),
        "clean_restore": True,
    })

    assert not report["success"]
    assert "rollback failed" in report["errors"][-1]
    assert next(tmp_path.glob(".backer-restore-*/keep.txt")).read_text() == "keep"


def test_clean_restore_refuses_filesystem_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _RestoreBackend(validation_success=True)
    agent = _agent(tmp_path)
    monkeypatch.setattr("backer.core.runner.get_backend", lambda *_: backend)
    monkeypatch.setattr(agent, "_report_progress", lambda **_: None)
    monkeypatch.setattr(agent, "_get_client", lambda: type("Client", (), {"post": lambda *_a, **_k: None})())

    report = agent.execute_restore({
        "run_id": "run-1",
        "job_name": "job",
        "backend": "kopia",
        "source_path": str(tmp_path / "repo"),
        "destination_path": tmp_path.anchor,
        "clean_restore": True,
    })

    assert not report["success"]
    assert "filesystem root" in report["errors"][0]
    assert backend.dry_runs == []


def test_clean_restore_refuses_symlink_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    original = target / "keep.txt"
    original.write_text("keep")
    destination = tmp_path / "restore"
    try:
        destination.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation requires Windows developer mode or privilege")
    backend = _RestoreBackend(validation_success=True)
    agent = _agent(tmp_path)
    monkeypatch.setattr("backer.core.runner.get_backend", lambda *_: backend)
    monkeypatch.setattr(agent, "_report_progress", lambda **_: None)
    monkeypatch.setattr(agent, "_get_client", lambda: type("Client", (), {"post": lambda *_a, **_k: None})())

    report = agent.execute_restore({
        "run_id": "run-1",
        "job_name": "job",
        "backend": "kopia",
        "source_path": str(tmp_path / "repo"),
        "destination_path": str(destination),
        "clean_restore": True,
    })

    assert not report["success"]
    assert original.read_text() == "keep"
    # Kopia validates via list_snapshots (it refuses dry-run restores), and the
    # symlink refusal happens before that real restore is ever invoked.
    assert backend.dry_runs == []


def test_gui_service_uses_shared_agent_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AgentService("http://example.test", "agent", "secret", tools_dir=tmp_path / "tools")
    calls: list[bool] = []
    monkeypatch.setattr(service, "_execute_with_shared_agent", lambda _payload, restore: calls.append(restore))

    service._execute_restore({"dry_run": False})

    assert calls == [True]


def test_failed_backup_run_is_recorded_with_its_error(tmp_path: Path) -> None:
    """A failing job must not be invisible to a second machine reading the repo."""
    agent = _agent(tmp_path)
    agent._write_metadata_to_path(
        tmp_path / "repository",
        "photos",
        "run-1",
        "/photos",
        "kopia",
        SimpleNamespace(
            success=False, bytes_transferred=0, files_transferred=0,
            errors=["repository unavailable"],
        ),
        datetime.now(),
        datetime.now(),
        None,
    )

    metadata = RepositoryMetadata(tmp_path / "repository")
    runs = metadata.get_job_runs("photos")
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["errors"] == ["repository unavailable"]


def test_execute_backup_writes_repo_metadata_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The end-of-execute_backup metadata write must not be gated on success."""
    agent = _agent(tmp_path)
    dest = tmp_path / "repository"

    failing_backend = SimpleNamespace(
        check_available=lambda: (True, "ready"),
        backup=lambda **_: SimpleNamespace(
            success=False, bytes_transferred=0, files_transferred=0,
            errors=["kopia snapshot create failed"], output="boom", metadata={},
            return_code=1,
        ),
    )
    monkeypatch.setattr("backer.core.runner.get_backend", lambda *_a, **_k: failing_backend)
    monkeypatch.setattr(agent, "_report_progress", lambda **_: None)
    monkeypatch.setattr(agent, "_get_client", lambda: type("Client", (), {"post": lambda *_a, **_k: None})())

    report = agent.execute_backup({
        "run_id": "run-1",
        "job_name": "photos",
        "source_path": str(tmp_path / "source"),
        "destination_path": str(dest),
    })

    assert not report["success"]
    metadata = RepositoryMetadata(dest)
    runs = metadata.get_job_runs("photos")
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["errors"] == ["kopia snapshot create failed"]


def test_repo_metadata_write_failure_is_logged_not_swallowed_silently(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    agent = _agent(tmp_path)

    # Force the failure deterministically: point dest_path at a file so
    # RepositoryMetadata can't create its metadata directory under it.
    blocked = tmp_path / "blocked_repo"
    blocked.write_text("not a directory")

    with caplog.at_level("ERROR", logger="backer.client.agent"):
        agent._write_repo_metadata(
            job={"job_name": "photos", "run_id": "run-1", "source_path": "/photos"},
            dest_path=str(blocked),
            backend_name="kopia",
            result=SimpleNamespace(success=True, bytes_transferred=0, files_transferred=0, errors=[]),
            started_at=datetime.now(),
            finished_at=datetime.now(),
            snapshot_id=None,
        )

    assert any("Failed to write metadata" in record.message for record in caplog.records)


def _patch_etc_backer_exists(monkeypatch: pytest.MonkeyPatch, exists: bool) -> None:
    real_exists = pathlib.Path.exists

    def fake_exists(self: pathlib.Path) -> bool:
        if self == pathlib.Path("/etc/backer/config.yaml"):
            return exists
        return real_exists(self)

    monkeypatch.setattr(pathlib.Path, "exists", fake_exists)


def test_linux_config_dir_prefers_etc_backer_when_it_holds_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing root install (scripts/install-agent.sh) must keep working after upgrade."""
    monkeypatch.delenv("BACKER_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(client_agent.sys, "platform", "linux")
    _patch_etc_backer_exists(monkeypatch, exists=True)

    assert client_agent.get_config_dir() == Path("/etc/backer")
