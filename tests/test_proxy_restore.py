from __future__ import annotations

import io
import tarfile
from pathlib import Path

from backer.backends.base import BackupDestination
from backer.backends.proxy import ProxyBackend


def _archive(files: dict[str, bytes]) -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return payload.getvalue()


def _backend_with_archive(monkeypatch, archive: bytes) -> ProxyBackend:
    backend = ProxyBackend({"location": "proxy://backer.example/repo/repo-1"})

    class Response:
        status_code = 200
        text = ""

        def iter_content(self, chunk_size: int):
            yield archive

    monkeypatch.setattr(backend, "_request", lambda *_args, **_kwargs: Response())
    return backend


def test_proxy_restore_keeps_existing_destination_when_archive_is_invalid(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "restore"
    destination.mkdir()
    (destination / "keep.txt").write_text("original")
    backend = _backend_with_archive(monkeypatch, _archive({"../escape.txt": b"unsafe"}))

    result = backend.restore(BackupDestination("proxy://backer.example/repo/repo-1/Agents/photos"), destination)

    assert not result.success
    assert (destination / "keep.txt").read_text() == "original"
    assert not (tmp_path / "escape.txt").exists()


def test_proxy_restore_merges_by_swapping_a_complete_candidate(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "restore"
    destination.mkdir()
    (destination / "unchanged.txt").write_text("keep")
    (destination / "replace.txt").write_text("old")
    backend = _backend_with_archive(monkeypatch, _archive({"replace.txt": b"new", "nested/file.txt": b"added"}))

    result = backend.restore(BackupDestination("proxy://backer.example/repo/repo-1/Agents/photos"), destination)

    assert result.success
    assert (destination / "unchanged.txt").read_text() == "keep"
    assert (destination / "replace.txt").read_text() == "new"
    assert (destination / "nested/file.txt").read_text() == "added"


def test_proxy_restore_copy_failure_does_not_overwrite_destination(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "restore"
    destination.mkdir()
    (destination / "keep.txt").write_text("original")
    backend = _backend_with_archive(monkeypatch, _archive({"keep.txt": b"new"}))
    def fail_copy(*_args, **_kwargs):
        raise OSError("copy failed")

    monkeypatch.setattr("backer.backends.proxy.shutil.copytree", fail_copy)

    result = backend.restore(BackupDestination("proxy://backer.example/repo/repo-1/Agents/photos"), destination)

    assert not result.success
    assert (destination / "keep.txt").read_text() == "original"
