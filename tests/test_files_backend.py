import json
import os
import threading
from pathlib import Path

from backer.backends.base import BackupDestination, BackupSource
from backer.backends.files import FilesBackend


def _job_destination(repo: Path) -> BackupDestination:
    return BackupDestination(str(repo / "Agents" / "photos"))


def test_files_snapshots_are_immutable_and_restoreable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("one")
    backend = FilesBackend({"repository_id": "repo-1", "snapshot_id": "run-1"})

    assert backend.init_repo(BackupDestination(str(repo))).success
    first = backend.backup(BackupSource(source), _job_destination(repo))
    assert first.success
    (source / "a.txt").write_text("two")
    backend.config["snapshot_id"] = "run-2"
    assert backend.backup(BackupSource(source), _job_destination(repo)).success

    snapshots = backend.list_snapshots(_job_destination(repo))
    assert [item["id"] for item in snapshots] == ["run-2", "run-1"]
    restore = tmp_path / "restore"
    assert backend.restore(_job_destination(repo), restore, snapshot="run-1").success
    assert (restore / "a.txt").read_text() == "one"


def test_files_snapshot_preserves_deleted_source_files(tmp_path: Path) -> None:
    repo, source = tmp_path / "repo", tmp_path / "source"
    source.mkdir()
    (source / "kept.txt").write_text("kept")
    (source / "deleted.txt").write_text("old")
    backend = FilesBackend({"snapshot_id": "first"})
    assert backend.init_repo(BackupDestination(str(repo))).success
    assert backend.backup(BackupSource(source), _job_destination(repo)).success
    (source / "deleted.txt").unlink()
    backend.config["snapshot_id"] = "second"
    assert backend.backup(BackupSource(source), _job_destination(repo)).success

    restore = tmp_path / "restore"
    assert backend.restore(_job_destination(repo), restore, snapshot="first").success
    assert (restore / "deleted.txt").read_text() == "old"
    assert backend.restore(_job_destination(repo), tmp_path / "latest", snapshot="second").success
    assert not (tmp_path / "latest" / "deleted.txt").exists()


def test_files_manifest_uses_trusted_source_identity(tmp_path: Path) -> None:
    repo, staging = tmp_path / "repo", tmp_path / "temporary-staging"
    staging.mkdir()
    (staging / "file").write_text("content")
    backend = FilesBackend({"snapshot_id": "run", "source_path": "C:/Users/matt/Documents"})
    assert backend.init_repo(BackupDestination(str(repo))).success
    assert backend.backup(BackupSource(staging), _job_destination(repo)).success

    assert backend.list_snapshots(_job_destination(repo))[0]["paths"] == ["C:/Users/matt/Documents"]


def test_files_backend_rejects_symlinks_and_never_lists_partial(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_text("content")
    backend = FilesBackend({"snapshot_id": "run-1"})
    assert backend.init_repo(BackupDestination(str(repo))).success
    (repo / "Agents" / "photos" / "snapshots" / ".partial-stale").mkdir(parents=True)
    assert backend.list_snapshots(_job_destination(repo)) == []
    link = source / "link"
    try:
        link.symlink_to(source / "file")
    except OSError:
        return
    result = backend.backup(BackupSource(source), _job_destination(repo))
    assert not result.success
    assert "Symlinks are not supported" in result.errors[0]


def test_files_backend_detects_corruption_and_refuses_conflicting_retry(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_text("one")
    backend = FilesBackend({"snapshot_id": "run"})
    assert backend.init_repo(BackupDestination(str(repo))).success
    assert backend.backup(BackupSource(source), _job_destination(repo)).success
    (source / "file").write_text("two")
    assert not backend.backup(BackupSource(source), _job_destination(repo)).success
    snapshot_file = repo / "Agents" / "photos" / "snapshots" / "run" / "contents" / "file"
    snapshot_file.write_text("corrupt")
    assert not backend.check(_job_destination(repo)).success
    assert not backend.restore(_job_destination(repo), tmp_path / "restore", snapshot="run").success


def test_restore_validates_every_file_before_writing_destination(tmp_path: Path) -> None:
    repo, source, restore = tmp_path / "repo", tmp_path / "source", tmp_path / "restore"
    source.mkdir()
    (source / "first").write_text("first")
    (source / "second").write_text("second")
    backend = FilesBackend({"snapshot_id": "run"})
    assert backend.init_repo(BackupDestination(str(repo))).success
    assert backend.backup(BackupSource(source), _job_destination(repo)).success
    (repo / "Agents" / "photos" / "snapshots" / "run" / "contents" / "second").write_text("corrupt")

    result = backend.restore(_job_destination(repo), restore, snapshot="run")

    assert not result.success
    assert not restore.exists()


def test_files_restore_rejects_manifest_and_include_traversal(tmp_path: Path) -> None:
    repo, source = tmp_path / "repo", tmp_path / "source"
    source.mkdir()
    (source / "file").write_text("content")
    backend = FilesBackend({"snapshot_id": "run"})
    assert backend.init_repo(BackupDestination(str(repo))).success
    assert backend.backup(BackupSource(source), _job_destination(repo)).success

    traversal = backend.restore(_job_destination(repo), tmp_path / "restore", snapshot="run", include_path="../file")
    assert not traversal.success
    manifest = repo / "Agents" / "photos" / "snapshots" / "run" / "manifest.json"
    value = json.loads(manifest.read_text())
    value["files"][0]["path"] = "../outside"
    manifest.write_text(json.dumps(value))
    assert not backend.restore(_job_destination(repo), tmp_path / "restore", snapshot="run").success


def test_files_snapshot_selectors_must_be_completed_opaque_components(tmp_path: Path) -> None:
    repo, source = tmp_path / "repo", tmp_path / "source"
    source.mkdir()
    (source / "file").write_text("content")
    backend = FilesBackend({"snapshot_id": "run"})
    assert backend.init_repo(BackupDestination(str(repo))).success
    assert backend.backup(BackupSource(source), _job_destination(repo)).success

    assert not backend.restore(_job_destination(repo), tmp_path / "restore", snapshot="run/other").success
    assert not backend.restore(_job_destination(repo), tmp_path / "restore", snapshot=".partial-run").success


def test_files_backend_rejects_root_and_repository_overlap(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    backend = FilesBackend({"snapshot_id": "run"})
    assert backend.init_repo(BackupDestination(str(repo))).success
    root_result = backend.backup(BackupSource(Path(tmp_path.anchor)), _job_destination(repo))
    overlap_result = backend.backup(BackupSource(repo), _job_destination(repo))
    assert not root_result.success
    assert not overlap_result.success


def test_files_restore_rejects_repository_parent(tmp_path: Path) -> None:
    repo, source = tmp_path / "repo", tmp_path / "source"
    source.mkdir()
    (source / "file").write_text("content")
    backend = FilesBackend({"snapshot_id": "run"})
    assert backend.init_repo(BackupDestination(str(repo))).success
    assert backend.backup(BackupSource(source), _job_destination(repo)).success

    result = backend.restore(_job_destination(repo), tmp_path, snapshot="run")

    assert not result.success
    assert "must not overlap" in result.errors[0]


def test_files_restore_does_not_follow_existing_output_symlink(tmp_path: Path) -> None:
    repo, source, restore = tmp_path / "repo", tmp_path / "source", tmp_path / "restore"
    source.mkdir()
    restore.mkdir()
    (source / "file").write_text("safe")
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    backend = FilesBackend({"snapshot_id": "run"})
    assert backend.init_repo(BackupDestination(str(repo))).success
    assert backend.backup(BackupSource(source), _job_destination(repo)).success
    try:
        (restore / "file").symlink_to(outside)
    except OSError:
        return

    result = backend.restore(_job_destination(repo), restore, snapshot="run")

    assert not result.success
    assert outside.read_text() == "unchanged"


def test_cancelled_backup_leaves_no_listable_partial_snapshot(tmp_path: Path) -> None:
    repo, source = tmp_path / "repo", tmp_path / "source"
    source.mkdir()
    (source / "first").write_text("one")
    (source / "second").write_text("two")
    cancelled = threading.Event()
    backend = FilesBackend({"snapshot_id": "run", "cancel_event": cancelled})
    assert backend.init_repo(BackupDestination(str(repo))).success

    result = backend.backup(
        BackupSource(source), _job_destination(repo), progress_callback=lambda **_kwargs: cancelled.set()
    )

    assert not result.success
    assert backend.list_snapshots(_job_destination(repo)) == []
    assert not list((repo / "Agents" / "photos" / "snapshots").glob(".partial-*"))


def test_cancelled_restore_rolls_back_existing_files(tmp_path: Path) -> None:
    repo, source, restore = tmp_path / "repo", tmp_path / "source", tmp_path / "restore"
    source.mkdir()
    restore.mkdir()
    (source / "first").write_text("new-first")
    (source / "second").write_text("new-second")
    (restore / "first").write_text("old-first")
    (restore / "second").write_text("old-second")
    cancelled = threading.Event()
    backend = FilesBackend({"snapshot_id": "run", "cancel_event": cancelled})
    assert backend.init_repo(BackupDestination(str(repo))).success
    assert backend.backup(BackupSource(source), _job_destination(repo)).success
    cancelled.clear()

    result = backend.restore(
        _job_destination(repo), restore, snapshot="run", progress_callback=lambda **_kwargs: cancelled.set()
    )

    assert not result.success
    assert (restore / "first").read_text() == "old-first"
    assert (restore / "second").read_text() == "old-second"


def test_files_backend_prune_keeps_one_snapshot(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source = tmp_path / "source"
    source.mkdir()
    backend = FilesBackend()
    assert backend.init_repo(BackupDestination(str(repo))).success
    for snapshot in ("one", "two"):
        (source / "file").write_text(snapshot)
        backend.config["snapshot_id"] = snapshot
        assert backend.backup(BackupSource(source), _job_destination(repo)).success
    assert backend.prune(_job_destination(repo), keep_last=0).success
    assert len(backend.list_snapshots(_job_destination(repo))) == 1


def test_files_retention_keeps_requested_calendar_generations() -> None:
    rows = [
        {"full_id": "new", "timestamp": "2026-03-03T00:00:00Z"},
        {"full_id": "same-day", "timestamp": "2026-03-03T00:00:00Z"},
        {"full_id": "old-day", "timestamp": "2026-03-02T00:00:00Z"},
        {"full_id": "older", "timestamp": "2026-02-01T00:00:00Z"},
    ]

    candidates = FilesBackend._prune_candidates(
        rows, {"last": None, "daily": 2, "weekly": None, "monthly": None, "yearly": None}
    )

    assert [row["full_id"] for row in candidates] == ["same-day", "older"]


def test_files_prune_revalidates_and_ignores_foreign_or_symlinked_entries(tmp_path: Path, monkeypatch) -> None:
    repo, source = tmp_path / "repo", tmp_path / "source"
    source.mkdir()
    backend = FilesBackend()
    assert backend.init_repo(BackupDestination(str(repo))).success
    for snapshot in ("one", "two"):
        (source / "file").write_text(snapshot)
        backend.config["snapshot_id"] = snapshot
        assert backend.backup(BackupSource(source), _job_destination(repo)).success
    snapshots = repo / "Agents" / "photos" / "snapshots"
    (snapshots / "foreign").mkdir()
    try:
        (snapshots / "linked").symlink_to(snapshots / "one", target_is_directory=True)
    except OSError:
        pass
    original = backend.list_snapshots
    calls = 0

    def changed_destination(destination):
        nonlocal calls
        calls += 1
        rows = original(destination)
        return rows[:-1] if calls == 2 else rows

    monkeypatch.setattr(backend, "list_snapshots", changed_destination)
    result = backend.prune(_job_destination(repo), keep_last=1)

    assert not result.success
    assert (snapshots / "foreign").exists()
    assert (snapshots / "one").exists() and (snapshots / "two").exists()


def test_files_prune_never_deletes_last_viable_snapshot(tmp_path: Path) -> None:
    repo, source = tmp_path / "repo", tmp_path / "source"
    source.mkdir()
    (source / "file").write_text("only")
    backend = FilesBackend({"snapshot_id": "only"})
    assert backend.init_repo(BackupDestination(str(repo))).success
    assert backend.backup(BackupSource(source), _job_destination(repo)).success

    assert backend.prune(_job_destination(repo), keep_last=0).success
    assert [row["id"] for row in backend.list_snapshots(_job_destination(repo))] == ["only"]


def test_files_prune_does_not_delete_hash_invalid_snapshot(tmp_path: Path) -> None:
    repo, source = tmp_path / "repo", tmp_path / "source"
    source.mkdir()
    backend = FilesBackend()
    assert backend.init_repo(BackupDestination(str(repo))).success
    for snapshot in ("old", "new"):
        (source / "file").write_text(snapshot)
        backend.config["snapshot_id"] = snapshot
        assert backend.backup(BackupSource(source), _job_destination(repo)).success
    (repo / "Agents" / "photos" / "snapshots" / "old" / "contents" / "file").write_text("corrupt")

    assert backend.prune(_job_destination(repo), keep_last=1).success
    assert (repo / "Agents" / "photos" / "snapshots" / "old").exists()


def test_files_marker_id_is_enforced_for_every_operation(tmp_path: Path) -> None:
    repo, source = tmp_path / "repo", tmp_path / "source"
    source.mkdir()
    (source / "file").write_text("content")
    creator = FilesBackend({"repository_id": "right", "snapshot_id": "run"})
    assert creator.init_repo(BackupDestination(str(repo))).success
    assert creator.backup(BackupSource(source), _job_destination(repo)).success
    backend = FilesBackend({"repository_id": "wrong"})

    try:
        backend.list_snapshots(_job_destination(repo))
    except ValueError as error:
        assert "does not match" in str(error)
    else:
        raise AssertionError("marker mismatch must fail listing")
    assert not backend.restore(_job_destination(repo), tmp_path / "restore", snapshot="run").success
    assert not backend.check(_job_destination(repo)).success
    assert not backend.prune(_job_destination(repo), keep_last=1).success


def test_files_backend_treats_windows_junctions_as_links(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(os.path, "isjunction", lambda path: Path(path) == tmp_path, raising=False)

    assert FilesBackend._is_link(tmp_path)


def test_job_identity_prevents_sanitized_folder_collision(tmp_path: Path) -> None:
    repo, source = tmp_path / "repo", tmp_path / "source"
    source.mkdir()
    (source / "file").write_text("content")
    creator = FilesBackend({"snapshot_id": "run", "job_name": "sales:daily"})
    assert creator.init_repo(BackupDestination(str(repo))).success
    assert creator.backup(BackupSource(source), _job_destination(repo)).success
    collided = FilesBackend({"job_name": "sales?daily"})

    try:
        collided.list_snapshots(_job_destination(repo))
    except ValueError as error:
        assert "job identity" in str(error)
    else:
        raise AssertionError("job-folder collision must fail closed")
    assert not collided.restore(_job_destination(repo), tmp_path / "restore", snapshot="run").success
    assert not collided.check(_job_destination(repo)).success
    assert not collided.prune(_job_destination(repo), keep_last=1).success


def test_files_prune_is_scoped_to_source_path(tmp_path: Path) -> None:
    repo, source = tmp_path / "repo", tmp_path / "source"
    source.mkdir()
    backend = FilesBackend()
    assert backend.init_repo(BackupDestination(str(repo))).success
    for snapshot, identity in (("old-a", "/source/a"), ("only-b", "/source/b"), ("new-a", "/source/a")):
        (source / "file").write_text(snapshot)
        backend.config.update(snapshot_id=snapshot, source_path=identity)
        assert backend.backup(BackupSource(source), _job_destination(repo)).success

    result = backend.prune(_job_destination(repo), keep_last=1, source_path="/source/a")

    assert result.success
    snapshots = repo / "Agents" / "photos" / "snapshots"
    assert not (snapshots / "old-a").exists()
    assert (snapshots / "only-b").exists()
    assert (snapshots / "new-a").exists()
