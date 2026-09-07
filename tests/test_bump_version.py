"""Release version bump behavior."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("bump_version", ROOT / "scripts/bump_version.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def setup(root: Path, changelog: bytes = b"## 0.9.0\n") -> list[Path]:
    (root / "src/backer").mkdir(parents=True)
    (root / "installer").mkdir()
    (root / "desktop/Backer.Desktop").mkdir(parents=True)
    (root / "android/app").mkdir(parents=True)
    (root / "CHANGELOG.md").write_bytes(changelog)
    files = {
        "pyproject.toml": b'[project]\nversion = "0.8.0"\n',
        "src/backer/_version.py": b'__version__ = "0.8.0"\n',
        "installer/backer-agent.iss": b'#define MyAppVersion "0.8.0"\n',
        "desktop/Backer.Desktop/Backer.Desktop.csproj": b"<Project>\n  <Version>0.8.0</Version>\n</Project>\n",
        "android/app/build.gradle.kts": b'versionCode = 800\nversionName = "0.8.0"\n',
    }
    for name, data in files.items():
        (root / name).write_bytes(data)
    return [root / name for name in files]


def run(module, root: Path, version: str) -> int:
    module.ROOT = root
    return module.main([version])


def test_bump_updates_every_version_site_and_android_code(tmp_path: Path) -> None:
    files = setup(tmp_path, b"## 2.34.5\n")

    assert run(load(), tmp_path, "2.34.5") == 0

    assert b'version = "2.34.5"' in files[0].read_bytes()
    assert b'__version__ = "2.34.5"' in files[1].read_bytes()
    assert b'#define MyAppVersion "2.34.5"' in files[2].read_bytes()
    assert b"<Version>2.34.5</Version>" in files[3].read_bytes()
    assert b"versionCode = 23405" in files[4].read_bytes()
    assert b'versionName = "2.34.5"' in files[4].read_bytes()


def test_bump_preserves_crlf_and_non_version_bytes(tmp_path: Path) -> None:
    files = setup(tmp_path, b"intro\r\n## 0.9.0\r\n")
    files[0].write_bytes(b'# keep\r\n[project]\r\nversion = "0.8.0"\r\n')
    files[4].write_bytes(b'// keep\r\nversionCode = 800\r\nversionName = "0.8.0"\r\n')
    before = {path: path.read_bytes() for path in files}

    assert run(load(), tmp_path, "0.9.0") == 0

    assert files[0].read_bytes() == before[files[0]].replace(b"0.8.0", b"0.9.0")
    assert files[4].read_bytes() == before[files[4]].replace(b"800", b"900").replace(b"0.8.0", b"0.9.0")


def test_invalid_version_or_missing_heading_writes_nothing(tmp_path: Path) -> None:
    files = setup(tmp_path, b"## 0.8.0\n")
    before = {path: path.read_bytes() for path in files}

    assert run(load(), tmp_path, "1.2") == 2
    assert run(load(), tmp_path, "0.9.0") == 2
    assert {path: path.read_bytes() for path in files} == before


def test_bad_existing_version_pattern_writes_nothing(tmp_path: Path) -> None:
    files = setup(tmp_path)
    files[2].write_bytes(b'#define MyAppVersion "not-a-version"\n')
    before = {path: path.read_bytes() for path in files}

    assert run(load(), tmp_path, "0.9.0") == 2

    assert {path: path.read_bytes() for path in files} == before


def test_bump_uses_no_persistent_lock_file() -> None:
    assert "/.backer-bump-version.lock" not in (ROOT / ".gitignore").read_text(encoding="utf-8")
