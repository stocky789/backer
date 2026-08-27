from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backer.backends.base import BackupDestination
from backer.backends.restic import ResticBackend


def _restore_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, include_path: str | None) -> list[str]:
    backend = ResticBackend()
    monkeypatch.setattr(backend, "_get_binary", lambda: Path("/usr/bin/restic"))
    calls: list[list[str]] = []

    def run(command: list[str], **_: object) -> MagicMock:
        calls.append(command)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("backer.backends.restic.subprocess.run", run)
    source = tmp_path / "source"
    backend.restore(
        BackupDestination(path=str(tmp_path / "repo")),
        source,
        original_source_path=str(source),
        include_path=include_path,
    )
    return calls[0]


def test_in_place_partial_restore_keeps_source_prefix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    command = _restore_command(monkeypatch, tmp_path, "reports/2026")

    assert command[command.index("--target") + 1] == "/"
    assert command[command.index("--include") + 1] == f"{tmp_path}/source/reports/2026"


def test_in_place_full_restore_still_includes_original_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    command = _restore_command(monkeypatch, tmp_path, None)

    assert command[command.index("--include") + 1] == f"{tmp_path}/source"


def test_in_place_partial_restore_rejects_traversal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="relative"):
        _restore_command(monkeypatch, tmp_path, "reports/../../outside")


@pytest.mark.parametrize(
    ("output", "matched_items", "bytes_restored"),
    [
        ("Summary: Restored 6 / 2 files/dirs (66 B / 66 B) in 0:00", 6, 66),
        ("Summary: Restored 1 files/dirs (0 B) in 0:00, skipped 3 files/dirs 12 B", 1, 0),
        ("Summary: Restored 0 files/dirs (0 B) in 0:00", 0, 0),
        ("Summary: Restored 2 files/dirs (1 TiB) in 0:00", 2, 1024**4),
    ],
)
def test_restore_parses_summary_match_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, output: str, matched_items: int, bytes_restored: int
) -> None:
    backend = ResticBackend()
    monkeypatch.setattr(backend, "_get_binary", lambda: Path("/usr/bin/restic"))
    monkeypatch.setattr(
        "backer.backends.restic.subprocess.run",
        lambda *_args, **_kwargs: MagicMock(returncode=0, stdout=output, stderr=""),
    )

    result = backend.restore(BackupDestination(path=str(tmp_path / "repo")), tmp_path / "restore")

    assert result.metadata["matched_items"] == matched_items
    assert result.files_transferred == matched_items
    assert result.bytes_transferred == bytes_restored


def test_resolve_latest_snapshot_returns_full_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backend = ResticBackend()
    snapshot_id = "a" * 64
    monkeypatch.setattr(backend, "_get_binary", lambda: Path("/usr/bin/restic"))
    monkeypatch.setattr(
        "backer.backends.restic.subprocess.run",
        lambda command, **_kwargs: (
            MagicMock(returncode=0, stdout=f'{{"id": "{snapshot_id}"}}', stderr="")
            if command == ["/usr/bin/restic", "snapshots", "latest", "--repo", str(tmp_path / "repo"), "--json"]
            else pytest.fail(f"Unexpected command: {command}")
        ),
    )

    assert backend.resolve_latest_snapshot(BackupDestination(path=str(tmp_path / "repo"))) == snapshot_id


@pytest.mark.parametrize("stdout", ["", "[]", "[{}, {}]"])
def test_resolve_latest_snapshot_rejects_invalid_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdout: str
) -> None:
    backend = ResticBackend()
    monkeypatch.setattr(backend, "_get_binary", lambda: Path("/usr/bin/restic"))
    monkeypatch.setattr(
        "backer.backends.restic.subprocess.run",
        lambda *_args, **_kwargs: MagicMock(returncode=0, stdout=stdout, stderr=""),
    )

    with pytest.raises(RuntimeError, match="Failed to resolve latest Restic snapshot"):
        backend.resolve_latest_snapshot(BackupDestination(path=str(tmp_path / "repo")))
