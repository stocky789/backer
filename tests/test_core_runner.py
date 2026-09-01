import threading
from datetime import datetime
from pathlib import Path

import pytest

from backer.backends.base import BackendResult, OperationType
from backer.client.agent import BackerAgent
from backer.core import runner


class _Backend:
    def check_available(self):
        return True, "ready"

    def backup(self, **_):
        return BackendResult(True, OperationType.BACKUP, datetime.now(), datetime.now())

    def restore(self, **_):
        return BackendResult(True, OperationType.RESTORE, datetime.now(), datetime.now())


def test_proxy_backup_passes_both_agent_credentials(tmp_path: Path, monkeypatch):
    """Dropping either proxy credential prevents server-managed local backups."""
    options = []
    monkeypatch.setattr(runner, "get_backend", lambda _name, value: options.append(value) or _Backend())

    runner.run_backup(
        {"job_name": "job", "source_path": str(tmp_path), "destination_path": "proxy://repo"},
        agent_credentials=("agent", "secret"),
    )

    assert options == [{"client_id": "agent", "client_secret": "secret", "location": "proxy://repo"}]


def test_backup_passes_progress_callback_without_inspecting_backend_signature(tmp_path: Path, monkeypatch):
    """A backend can learn progress from frames; a parameter-name probe must not suppress it."""
    received = []

    class Backend(_Backend):
        def backup(self, **kwargs):
            received.append(kwargs["progress_callback"])
            return super().backup(**kwargs)

    monkeypatch.setattr(runner, "get_backend", lambda *_: Backend())

    runner.run_backup({"job_name": "job", "source_path": str(tmp_path), "destination_path": str(tmp_path / "repo")})

    assert len(received) == 1


def test_proxy_restore_passes_both_agent_credentials(tmp_path: Path, monkeypatch):
    """Dropping either proxy credential prevents server-managed local restores."""
    options = []
    monkeypatch.setattr(runner, "get_backend", lambda _name, value: options.append(value) or _Backend())

    runner.run_restore(
        {"job_name": "job", "source_path": "proxy://repo", "destination_path": str(tmp_path)},
        agent_credentials=("agent", "secret"),
    )

    assert options == [{"client_id": "agent", "client_secret": "secret", "location": "proxy://repo"}]


def test_restore_passes_progress_callback_without_inspecting_backend_signature(tmp_path: Path, monkeypatch):
    """Restore frames must reach the caller even though support is learned at runtime."""
    received = []

    class Backend(_Backend):
        def restore(self, **kwargs):
            received.append(kwargs["progress_callback"])
            return super().restore(**kwargs)

    monkeypatch.setattr(runner, "get_backend", lambda *_: Backend())

    runner.run_restore({"job_name": "job", "source_path": "proxy://repo", "destination_path": str(tmp_path)})

    assert len(received) == 1


def test_clean_restore_cancellation_rolls_back_the_staged_original(tmp_path: Path, monkeypatch):
    destination = tmp_path / "restore"
    destination.mkdir()
    (destination / "original.txt").write_text("keep", encoding="utf-8")
    cancelled = threading.Event()

    class Backend(_Backend):
        def list_snapshots(self, _source):
            return [{"id": "snapshot"}]

        def restore(self, **kwargs):
            (kwargs["destination"] / "partial.txt").write_text("new", encoding="utf-8")
            cancelled.set()
            return BackendResult(True, OperationType.RESTORE, datetime.now(), datetime.now())

    monkeypatch.setattr(runner, "get_backend", lambda *_: Backend())

    report = runner.run_restore(
        {
            "job_name": "job",
            "source_path": str(tmp_path / "repo"),
            "destination_path": str(destination),
            "snapshot": "snapshot",
            "clean_restore": True,
            "repository_options": {"cancel_event": cancelled},
        }
    )

    assert report["cancelled"] and (destination / "original.txt").read_text(encoding="utf-8") == "keep"
    assert not (destination / "partial.txt").exists()


def test_preserved_clean_restore_keeps_the_replaced_destination(tmp_path: Path, monkeypatch):
    destination = tmp_path / "restore"
    destination.mkdir()
    (destination / "original.txt").write_text("keep", encoding="utf-8")

    class Backend(_Backend):
        def list_snapshots(self, _source):
            return [{"id": "snapshot"}]

        def restore(self, **kwargs):
            (kwargs["destination"] / "restored.txt").write_text("new", encoding="utf-8")
            return BackendResult(True, OperationType.RESTORE, datetime.now(), datetime.now())

    monkeypatch.setattr(runner, "get_backend", lambda *_: Backend())

    report = runner.run_restore(
        {
            "job_name": "job",
            "source_path": str(tmp_path / "repo"),
            "destination_path": str(destination),
            "snapshot": "snapshot",
            "clean_restore": True,
            "preserve_replaced": True,
        }
    )

    retained = Path(report["replaced_destination"])
    assert report["success"] and retained.name.startswith(".replaced-")
    assert (retained / "original.txt").read_text(encoding="utf-8") == "keep"
    assert (destination / "restored.txt").read_text(encoding="utf-8") == "new"


def test_backup_reports_interruption_once_before_reraising(tmp_path: Path, monkeypatch):
    """An interrupted Kopia process must create one failed result, not silently disappear or retry."""
    reports = []

    class Backend(_Backend):
        def backup(self, **kwargs):
            raise KeyboardInterrupt

    monkeypatch.setattr(runner, "get_backend", lambda *_: Backend())

    with pytest.raises(KeyboardInterrupt):
        runner.run_backup(
            {"job_name": "job", "source_path": str(tmp_path), "destination_path": str(tmp_path / "repo")},
            on_result=reports.append,
        )

    assert len(reports) == 1
    assert reports[0]["success"] is False
    assert reports[0]["errors"] == ["Backup interrupted"]


def test_one_backend_instance_per_run_and_agent_forwards_credentials(tmp_path: Path, monkeypatch):
    """Reusing a backend shares its stateful Kopia connection between runs."""
    instances = []
    monkeypatch.setattr(runner, "get_backend", lambda *_: instances.append(_Backend()) or instances[-1])
    job = {"job_name": "job", "source_path": str(tmp_path), "destination_path": "proxy://repo"}

    runner.run_backup(job)
    runner.run_backup(job)

    received = []
    monkeypatch.setattr("backer.client.agent.run_backup", lambda *_args, **kwargs: received.append(kwargs) or {})
    BackerAgent("http://example.test", "agent", "secret").execute_backup(job)

    assert len(instances) == 2
    assert instances[0] is not instances[1]
    assert received == [
        {
            "dry_run": False,
            "on_progress": received[0]["on_progress"],
            "on_result": received[0]["on_result"],
            "agent_credentials": ("agent", "secret"),
        }
    ]


def test_serverless_metadata_identifies_the_agent_mode(tmp_path: Path, monkeypatch) -> None:
    saved = []

    class Metadata:
        def __init__(self, _path):
            pass

        def is_initialized(self):
            return True

        def save_agent(self, **kwargs):
            saved.append(kwargs)

        def save_job_run(self, *_args, **_kwargs):
            return True

    result = BackendResult(True, OperationType.BACKUP, datetime.now(), datetime.now())
    monkeypatch.setattr(runner, "RepositoryMetadata", Metadata)

    runner._write_metadata_to_path(
        tmp_path,
        "nightly",
        "run",
        "/data",
        "kopia",
        result,
        datetime.now(),
        datetime.now(),
        None,
        "agent",
        {"serverless": True},
    )

    assert len(saved) == 1
    assert saved[0]["agent_id"] == "agent"
    assert saved[0]["agent_data"]["modes"] == ["serverless"]
