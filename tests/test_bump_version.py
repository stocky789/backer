"""Single-lock release version bump checks."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("bump_version", ROOT / "scripts/bump_version.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def setup(root, changelog="## 0.9.0\n"):
    (root / "src/backer").mkdir(parents=True)
    (root / "installer").mkdir()
    (root / "android/app").mkdir(parents=True)
    (root / "CHANGELOG.md").write_text(changelog)
    (root / "pyproject.toml").write_text('[project]\nversion = "0.8.0"\n')
    (root / "src/backer/_version.py").write_text('__version__ = "0.8.0"\n')
    (root / "installer/backer-agent.iss").write_text('#define MyAppVersion "0.8.0"\n')
    (root / "android/app/build.gradle.kts").write_text('versionCode = 800\nversionName = "0.8.0"\n')


def paths(root):
    return [
        root / rel
        for rel in (
            "pyproject.toml",
            "src/backer/_version.py",
            "installer/backer-agent.iss",
            "android/app/build.gradle.kts",
        )
    ]


def temps(root):
    return list(root.rglob(".backer-version-*"))


def run(m, root, arg):
    m.ROOT = root
    sys.argv = ["bump_version.py", arg]
    return m.main()


def journal(root):
    entries = []
    for path in paths(root):
        state = path.stat()
        data = path.read_bytes()
        entries.append(
            {
                "target": path.relative_to(root).as_posix(),
                "data": base64.b64encode(data).decode(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "mode": state.st_mode & 0o7777,
                "atime_ns": state.st_atime_ns,
                "mtime_ns": state.st_mtime_ns,
                "staged": None,
            }
        )
    return {"schema": 1, "entries": entries}


def test_bump_updates_all_four(tmp_path):
    setup(tmp_path)
    m = load()
    assert run(m, tmp_path, "0.9.0") == 0
    assert all(b"0.9.0" in p.read_bytes() for p in paths(tmp_path))
    assert b"versionCode = 900" in paths(tmp_path)[3].read_bytes()


def test_bump_preserves_crlf_and_unrelated_bytes(tmp_path):
    setup(tmp_path)
    before = {path: path.read_bytes() for path in paths(tmp_path)}
    assert run(load(), tmp_path, "0.9.0") == 0
    for path, data in before.items():
        assert path.read_bytes() == data.replace(b"0.8.0", b"0.9.0").replace(b"800", b"900")


def test_missing_changelog_does_not_touch_targets(tmp_path):
    setup(tmp_path, "## 0.8.0\n")
    before = {p: p.read_bytes() for p in paths(tmp_path)}
    assert run(load(), tmp_path, "0.9.0") == 2
    assert {p: p.read_bytes() for p in paths(tmp_path)} == before


def test_changed_target_between_transform_and_journal_fails_closed(tmp_path, monkeypatch):
    setup(tmp_path)
    m = load()
    before = {path: path.read_bytes() for path in paths(tmp_path)}
    real = m.read_file
    calls = 0

    def changed(path):
        nonlocal calls
        calls += 1
        data, state = real(path)
        if calls == 5:
            return data + b"external change", state
        return data, state

    monkeypatch.setattr(m, "read_file", changed)
    assert run(m, tmp_path, "0.9.0") == 1
    assert {path: path.read_bytes() for path in paths(tmp_path)} == before


def test_changed_target_metadata_between_transform_and_journal_fails_closed(tmp_path, monkeypatch):
    setup(tmp_path)
    m = load()
    real = m.read_file
    calls = 0

    def changed(path):
        nonlocal calls
        calls += 1
        if calls == 5:
            replacement = tmp_path / "same-bytes"
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)
        return real(path)

    monkeypatch.setattr(m, "read_file", changed)
    assert run(m, tmp_path, "0.9.0") == 1
    assert (tmp_path / m.LOCK).read_bytes() == b"0"


@pytest.mark.parametrize("at", range(4))
def test_failure_at_each_replace_restores_all(tmp_path, monkeypatch, at):
    setup(tmp_path)
    m = load()
    before = {p: p.read_bytes() for p in paths(tmp_path)}
    real = m.os.replace
    calls = 0

    def fail(src, dst):
        nonlocal calls
        if Path(dst) in paths(tmp_path):
            calls += 1
            if calls == at + 1:
                raise OSError("boom")
        real(src, dst)

    monkeypatch.setattr(m.os, "replace", fail)
    assert run(m, tmp_path, "0.9.0") == 1
    assert {p: p.read_bytes() for p in paths(tmp_path)} == before
    assert not temps(tmp_path)


def test_crash_recovers_on_next_invocation(tmp_path, monkeypatch):
    setup(tmp_path)
    m = load()
    before = {p: p.read_bytes() for p in paths(tmp_path)}
    real = m.os.replace
    calls = 0

    def crash(src, dst):
        nonlocal calls
        if Path(dst) in paths(tmp_path):
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
        real(src, dst)

    monkeypatch.setattr(m.os, "replace", crash)
    with pytest.raises(KeyboardInterrupt):
        run(m, tmp_path, "0.9.0")
    pending = json.loads((tmp_path / m.LOCK).read_bytes()[1:])
    assert set(pending) == {"schema", "entries"}
    assert all(entry["staged"] for entry in pending["entries"])
    monkeypatch.undo()
    assert run(load(), tmp_path, "bad") == 2
    assert {p: p.read_bytes() for p in paths(tmp_path)} == before
    assert not temps(tmp_path)


def test_recovery_preserves_original_bytes_mode_and_mtime(tmp_path):
    setup(tmp_path)
    m = load()
    for index, path in enumerate(paths(tmp_path)):
        os.chmod(path, 0o640 + index)
        os.utime(path, ns=(1_700_000_000_000_000_000 + index, 1_700_000_100_000_000_000 + index))
    original = journal(tmp_path)
    for path in paths(tmp_path):
        path.write_bytes(b"new\r\n")
    (tmp_path / m.LOCK).write_bytes(b"1" + json.dumps(original).encode())
    assert run(m, tmp_path, "bad") == 2
    for entry, path in zip(original["entries"], paths(tmp_path), strict=True):
        assert path.read_bytes() == base64.b64decode(entry["data"])
        assert path.stat().st_mode & 0o7777 == entry["mode"]
        assert path.stat().st_mtime_ns == entry["mtime_ns"]


def test_recovery_failure_retains_journal(tmp_path, monkeypatch):
    setup(tmp_path)
    m = load()
    original = journal(tmp_path)
    for path in paths(tmp_path):
        path.write_bytes(b"new")
    lock = tmp_path / m.LOCK
    lock.write_bytes(b"1" + json.dumps(original).encode())

    def fail(*_):
        raise OSError("replace failed")

    monkeypatch.setattr(m.os, "replace", fail)
    assert run(m, tmp_path, "0.9.0") == 1
    assert lock.read_bytes().startswith(b"1")
    assert not temps(tmp_path)


@pytest.mark.parametrize("payload", [b"1{", b"1[]"])
def test_corrupt_lock_fails_closed(tmp_path, payload):
    setup(tmp_path)
    before = {p: p.read_bytes() for p in paths(tmp_path)}
    (tmp_path / ".backer-bump-version.lock").write_bytes(payload)
    assert run(load(), tmp_path, "0.9.0") == 1
    assert {p: p.read_bytes() for p in paths(tmp_path)} == before


def test_oversized_lock_fails_closed(tmp_path):
    setup(tmp_path)
    before = {p: p.read_bytes() for p in paths(tmp_path)}
    (tmp_path / ".backer-bump-version.lock").write_bytes(b"1" + b"x" * (8 * 1024 * 1024 + 1))
    assert run(load(), tmp_path, "0.9.0") == 1
    assert {p: p.read_bytes() for p in paths(tmp_path)} == before


def test_hostile_target_in_journal_fails_closed(tmp_path):
    setup(tmp_path)
    m = load()
    j = journal(tmp_path)
    j["entries"][0]["target"] = "outside"
    (tmp_path / m.LOCK).write_bytes(b"1" + json.dumps(j).encode())
    assert run(m, tmp_path, "0.9.0") == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows handle boundary")
def test_windows_handle_opens_and_resolves_exact_target(tmp_path):
    setup(tmp_path)
    m = load()
    target = tmp_path / "pyproject.toml"
    handle = m._win_open(target, m.GENERIC_READ)
    try:
        assert m._win_final(m._win_api(), handle) == m._expected(target)
    finally:
        m._win_api().CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse boundary")
def test_windows_reparse_target_is_rejected(tmp_path):
    setup(tmp_path)
    m = load()
    target, link = tmp_path / "pyproject.toml", tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    with pytest.raises(m.TransactionError):
        m._win_open(link, m.GENERIC_READ)


def test_staging_descriptor_outside_repository_is_not_written(tmp_path, monkeypatch):
    setup(tmp_path)
    m = load()
    outside = tmp_path.parent / "outside"
    outside.write_bytes(b"outside")
    fd = os.open(outside, os.O_RDWR)
    monkeypatch.setattr(m.tempfile, "mkstemp", lambda **_: (fd, str(outside)))
    with pytest.raises(m.TransactionError):
        m.stage(tmp_path / "pyproject.toml", b"new", 0o644, 1, 1)
    assert outside.read_bytes() == b"outside"


def test_stage_failure_cleans_its_verified_temp(tmp_path, monkeypatch):
    setup(tmp_path)
    m = load()
    monkeypatch.setattr(m, "write_all_fd", lambda *_: (_ for _ in ()).throw(OSError("write failed")))
    with pytest.raises(OSError):
        m.stage(tmp_path / "pyproject.toml", b"new", 0o644, 1, 1)
    assert not temps(tmp_path)


def test_substituted_staging_path_is_retained(tmp_path):
    setup(tmp_path)
    m = load()
    staged = m.stage(tmp_path / "pyproject.toml", b"new", 0o644, 1, 1)
    os.unlink(staged.path)
    staged.path.write_bytes(b"attacker")
    m.cleanup_staged(staged)
    assert staged.path.read_bytes() == b"attacker"


def test_persistent_lock_is_gitignored():
    result = subprocess.run(["git", "check-ignore", "-q", ".backer-bump-version.lock"], cwd=ROOT, check=False)
    assert result.returncode == 0


def test_live_lock_is_busy_until_owner_exits(tmp_path):
    setup(tmp_path)
    m = load()
    m.ROOT = tmp_path
    with m.ReleaseLock():
        with pytest.raises(OSError):
            with m.ReleaseLock():
                pass


def test_crashed_process_releases_lock(tmp_path):
    setup(tmp_path)
    code = """import importlib.util, pathlib, sys, time
spec = importlib.util.spec_from_file_location('bump', sys.argv[1])
m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m; spec.loader.exec_module(m)
m.ROOT = pathlib.Path(sys.argv[2])
with m.ReleaseLock():
 print('ready', flush=True); time.sleep(30)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(ROOT / "scripts/bump_version.py"), str(tmp_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        assert run(load(), tmp_path, "bad") == 1
    finally:
        process.kill()
        process.wait()
    assert run(load(), tmp_path, "bad") == 2


def test_short_staging_write_is_completed(tmp_path, monkeypatch):
    setup(tmp_path)
    m = load()
    real = m.os.write

    def short(fd, data):
        return real(fd, data[: max(1, len(data) // 2)])

    monkeypatch.setattr(m.os, "write", short)
    assert run(m, tmp_path, "0.9.0") == 0
    assert all(b"0.9.0" in path.read_bytes() for path in paths(tmp_path))


def test_journal_save_and_clear_complete_short_writes(tmp_path, monkeypatch):
    setup(tmp_path)
    m = load()
    m.ROOT = tmp_path
    payload = journal(tmp_path)
    real = m.os.write

    def short(fd, data):
        return real(fd, data[: max(1, len(data) // 2)])

    with m.ReleaseLock() as lock:
        monkeypatch.setattr(m.os, "write", short)
        lock.save(payload)
        assert lock.load() == payload
        lock.clear()
        assert lock.load() is None


def test_short_journal_read_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "data"
    path.write_bytes(b"x")
    m = load()
    fd = os.open(path, os.O_RDONLY)
    try:
        monkeypatch.setattr(os, "read", lambda *_: b"")
        with pytest.raises(m.TransactionError):
            m._read_all(fd, 1)
    finally:
        os.close(fd)


@pytest.mark.parametrize("field,value", [("mode", "bad"), ("atime_ns", -1), ("mtime_ns", True)])
def test_invalid_journal_metadata_fails_before_restore(tmp_path, field, value):
    setup(tmp_path)
    m = load()
    entries = []
    for path in paths(tmp_path):
        data = path.read_bytes()
        entries.append(
            {
                "target": path.relative_to(tmp_path).as_posix(),
                "data": base64.b64encode(data).decode(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "mode": 0o644,
                "atime_ns": 1,
                "mtime_ns": 1,
                "staged": None,
            }
        )
    entries[0][field] = value
    (tmp_path / m.LOCK).write_bytes(b"1" + json.dumps({"schema": 1, "entries": entries}).encode())
    before = {path: path.read_bytes() for path in paths(tmp_path)}
    assert run(m, tmp_path, "0.9.0") == 1
    assert {path: path.read_bytes() for path in paths(tmp_path)} == before
