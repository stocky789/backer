from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from backer.backends.base import BackendResult, OperationType
from backer.server.retention import RetentionManager, RetentionPolicy


class Storage:
    def __init__(self, root: Path, runs: list[dict[str, str]]):
        self.job = {"name": "job", "repository_id": "repo", "retention": {"keep_last": 1}}
        self.repo = {"id": "repo", "name": "repo", "repo_type": "local", "share": str(root), "format": "files"}
        self.runs = runs

    def get_job(self, _name: str):
        return self.job

    def get_repository(self, _repo_id: str):
        return self.repo

    def get_job_runs(self, _name: str, limit: int = 1000):
        return self.runs[:limit]


def result(*, success: bool = True, ids: list[str] | None = None) -> BackendResult:
    now = datetime.now(UTC)
    return BackendResult(
        success=success,
        operation=OperationType.PRUNE,
        started_at=now,
        finished_at=now,
        metadata={"deleted_snapshot_ids": ids or []},
        errors=[] if success else ["failed"],
        return_code=0 if success else 1,
    )


def test_managed_files_retention_previews_exact_ids_before_db_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path, [
        {"run_id": "old", "snapshot_id": "old", "started_at": "2026-01-01T00:00:00+00:00"},
        {"run_id": "new", "snapshot_id": "new", "started_at": "2026-01-02T00:00:00+00:00"},
    ])
    calls: list[bool] = []

    class Backend:
        def list_snapshots(self, _destination):
            return [{"full_id": "old"}, {"full_id": "new"}]

        def prune(self, _destination, **kwargs):
            calls.append(kwargs["dry_run"])
            return result(ids=["old"])

    monkeypatch.setattr("backer.backends.registry.get_backend", lambda *_args: Backend())
    manager = RetentionManager(storage)  # type: ignore[arg-type]
    deleted: list[str] = []
    monkeypatch.setattr(manager, "_delete_run", lambda _job, run_id: deleted.append(run_id))

    assert [run["run_id"] for run in manager.apply_retention("job")] == ["old"]
    assert calls == [True, False]
    assert deleted == ["old"]


def test_managed_files_retention_refuses_candidate_mismatch_before_db_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path, [
        {"run_id": "old", "snapshot_id": "old", "started_at": "2026-01-01T00:00:00+00:00"},
        {"run_id": "new", "snapshot_id": "new", "started_at": "2026-01-02T00:00:00+00:00"},
    ])

    class Backend:
        def list_snapshots(self, _destination):
            return [{"full_id": "old"}, {"full_id": "new"}]

        def prune(self, _destination, **_kwargs):
            return result(ids=["new"])

    monkeypatch.setattr("backer.backends.registry.get_backend", lambda *_args: Backend())
    manager = RetentionManager(storage)  # type: ignore[arg-type]
    monkeypatch.setattr(manager, "_delete_run", lambda *_args: pytest.fail("DB history must survive"))

    with pytest.raises(RuntimeError, match="candidates did not match"):
        manager.apply_retention("job")


def test_retention_rejects_negative_values() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        RetentionPolicy(keep_last=-1)


def test_hypervisor_retention_keeps_db_history_when_storage_delete_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class HypervisorStorage:
        def get_hypervisor_job(self, _job_id: str):
            return {
                "id": "job", "name": "job", "repository_id": "repo", "hypervisor_id": "hyper",
                "retention": {"keep_last": 1},
            }

        def get_hypervisor_runs(self, **_kwargs):
            return [
                {"run_id": "old", "guest_id": "100", "started_at": "2026-01-01T00:00:00+00:00"},
                {"run_id": "new", "guest_id": "100", "started_at": "2026-01-02T00:00:00+00:00"},
            ]

        def get_repository(self, _repo_id: str):
            return {"id": "repo", "repo_type": "smb"}

        def get_hypervisor(self, _hypervisor_id: str):
            return {"name": "hyper"}

    manager = RetentionManager(HypervisorStorage())  # type: ignore[arg-type]
    monkeypatch.setattr(manager, "_delete_hypervisor_backup_files", lambda *_args: False)
    monkeypatch.setattr(manager, "_delete_hypervisor_run", lambda *_args: pytest.fail("DB history must survive"))

    with pytest.raises(RuntimeError, match="physical deletion was incomplete"):
        manager.apply_hypervisor_retention("job")


def test_hypervisor_nfs_delete_matches_seconds_not_the_whole_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dump = tmp_path / "dump"
    dump.mkdir()
    old = dump / "vzdump-qemu-100-2026_01_01-01_02_03.vma.zst"
    kept = dump / "vzdump-qemu-100-2026_01_01-09_08_07.vma.zst"
    old.write_text("old")
    kept.write_text("keep")
    manager = RetentionManager(object())  # type: ignore[arg-type]

    monkeypatch.setattr("backer.server.retention.tempfile.mkdtemp", lambda **_kwargs: str(tmp_path))
    monkeypatch.setattr(
        "backer.server.retention.subprocess.run",
        lambda command, **_kwargs: CompletedProcess(command, 0, "", ""),
    )

    assert manager._delete_files_from_nfs("server", "export", "dump", [("100", "2026_01_01-01_02_03")])
    assert not old.exists()
    assert kept.exists()
