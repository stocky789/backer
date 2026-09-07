import json
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


def test_direct_backend_uses_explicit_repository_format(monkeypatch):
    selected = []
    monkeypatch.setattr(runner, "get_backend", lambda name, options: selected.append((name, options)) or _Backend())

    runner._backend_for_location("/backup", {"format": "files"})

    assert selected == [("files", {"format": "files"})]


def test_files_backend_rejects_object_storage_before_writes():
    with pytest.raises(RuntimeError, match="do not support S3"):
        runner._backend_for_location("s3://bucket", {"format": "files"})


def test_backup_forwards_include_and_exclude_patterns(tmp_path: Path, monkeypatch):
    received = []

    class Backend(_Backend):
        def backup(self, **kwargs):
            received.append(kwargs["source"])
            return super().backup(**kwargs)

    monkeypatch.setattr(runner, "get_backend", lambda *_: Backend())
    runner.run_backup(
        {
            "job_name": "job",
            "source_path": str(tmp_path),
            "destination_path": str(tmp_path / "repo"),
            "includes": ["*.txt"],
            "excludes": ["tmp/*"],
        }
    )

    assert received[0].includes == ["*.txt"]
    assert received[0].excludes == ["tmp/*"]


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


def test_serverless_metadata_identifies_the_agent_mode(tmp_path: Path) -> None:
    result = BackendResult(True, OperationType.BACKUP, datetime.now(), datetime.now())

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

    agent = json.loads((tmp_path / ".backer" / "agents" / "agent.json").read_text(encoding="utf-8"))
    assert agent["agent_id"] == "agent"
    assert agent["modes"] == ["serverless"]


def test_sidecar_records_files_repository_format(tmp_path: Path) -> None:
    result = BackendResult(True, OperationType.BACKUP, datetime.now(), datetime.now())

    runner._write_metadata_to_path(
        tmp_path, "nightly", "run", "/data", "files", result, datetime.now(), datetime.now(), "run", "agent"
    )

    metadata = json.loads((tmp_path / ".backer" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["format"] == "files"


def test_sidecar_records_the_cause_not_kopias_progress_banner(tmp_path: Path) -> None:
    """`error` is what a UI shows; kopia's first error line is its 'Snapshotting ...' banner."""
    result = BackendResult(
        False,
        OperationType.BACKUP,
        datetime.now(),
        datetime.now(),
        errors=[
            "Snapshotting matt@host:/data ...",
            "encountered 2 errors:",
            "failed to prepare source: no such file or directory",
            "upload error: unsupported source",
        ],
        return_code=1,
    )

    runner._write_metadata_to_path(
        tmp_path, "nightly", "run", "/data", "kopia", result, datetime.now(), datetime.now(), None, "agent", None
    )

    record = json.loads((tmp_path / ".backer" / "jobs" / "nightly" / "runs" / "run.json").read_text(encoding="utf-8"))
    assert record["error"] == (
        "failed to prepare source: no such file or directory; upload error: unsupported source"
    )
    assert record["error_stage"] == "backup"


def _fake_s3(stored: dict[str, bytes]):
    class Sidecar:
        def __init__(self, *_: object) -> None:
            pass

        def get(self, key: str) -> bytes | None:
            return stored.get(key)

        def put_atomic(self, key: str, data: bytes) -> None:
            stored[key] = data

    return Sidecar


def test_s3_and_filesystem_sidecars_write_the_same_documents(tmp_path: Path, monkeypatch) -> None:
    """One run must produce one document set; the repository type only decides transport."""
    stored: dict[str, bytes] = {}
    monkeypatch.setattr("backer.serverless.s3_sidecar.S3Sidecar", _fake_s3(stored))
    result = BackendResult(True, OperationType.BACKUP, datetime.now(), datetime.now())
    result.bytes_transferred, result.files_transferred = 12, 3
    common = {
        "serverless": True,
        "run_id": "20260101T000000Z-agent123",
        "job_name": "nightly",
        "source_path": "/data",
        "excludes": ["*.tmp"],
        "schedule": {"cron": "0 2 * * *"},
        "retention": {"keep_latest": 3},
    }

    runner._write_repo_metadata(
        {**common, "repository_hint": {"type": "local"}},
        str(tmp_path),
        "kopia",
        result,
        datetime.now(),
        datetime.now(),
        "0123456789abcdef",
        "agent123",
    )
    runner._write_repo_metadata(
        {
            **common,
            "repository_hint": {"type": "s3", "bucket": "bucket", "endpoint": "https://s3.example"},
            "repository_options": {"s3": {"access_key_id": "a", "secret_access_key": "s"}},
        },
        "s3://bucket",
        "kopia",
        result,
        datetime.now(),
        datetime.now(),
        "0123456789abcdef",
        "agent123",
    )

    on_disk = {
        str(path.relative_to(tmp_path)).replace("\\", "/"): json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / ".backer").rglob("*.json")
    }
    from_s3 = {key: json.loads(value) for key, value in stored.items()}
    assert set(on_disk) == set(from_s3)
    for key, document in on_disk.items():
        assert set(document) == set(from_s3[key]), key
    run_key = ".backer/jobs/nightly/runs/20260101T000000Z-agent123.json"
    assert from_s3[run_key]["bytes_transferred"] == 12
    assert from_s3[run_key]["files_transferred"] == 3
    assert from_s3[run_key]["snapshot_id"] == "0123456789abcdef"
    assert from_s3[".backer/jobs/nightly/config.json"]["config"]["excludes"] == ["*.tmp"]
