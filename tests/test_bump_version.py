"""Single-lock release version bump checks."""

from __future__ import annotations

import importlib.util
import json
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


def run(m, root, arg):
    m.ROOT = root
    sys.argv = ["bump_version.py", arg]
    return m.main()


def test_bump_updates_all_four(tmp_path):
    setup(tmp_path)
    m = load()
    assert run(m, tmp_path, "0.9.0") == 0
    assert all(b"0.9.0" in p.read_bytes() for p in paths(tmp_path))
    assert b"versionCode = 900" in paths(tmp_path)[3].read_bytes()


def test_missing_changelog_does_not_touch_targets(tmp_path):
    setup(tmp_path, "## 0.8.0\n")
    before = {p: p.read_bytes() for p in paths(tmp_path)}
    assert run(load(), tmp_path, "0.9.0") == 2
    assert {p: p.read_bytes() for p in paths(tmp_path)} == before


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
    monkeypatch.undo()
    assert run(load(), tmp_path, "bad") == 2
    assert {p: p.read_bytes() for p in paths(tmp_path)} == before


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
    j = {
        "schema": 1,
        "committed": 0,
        "entries": [{"target": "outside", "data": "", "sha256": "x", "mode": 0, "mtime_ns": 0}] * 4,
    }
    (tmp_path / m.LOCK).write_bytes(b"1" + json.dumps(j).encode())
    assert run(m, tmp_path, "0.9.0") == 1


def test_source_declares_windows_handle_boundary():
    source = (ROOT / "scripts/bump_version.py").read_text()
    assert "FILE_FLAG_OPEN_REPARSE_POINT" in source and "GetFinalPathNameByHandleW" in source
