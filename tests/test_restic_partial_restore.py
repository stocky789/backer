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
