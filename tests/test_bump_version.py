"""Release version bump checks."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_bump_version():
    spec = importlib.util.spec_from_file_location("bump_version", ROOT / "scripts" / "bump_version.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_release_files(root: Path, changelog: str = "## 0.9.0\n") -> None:
    (root / "src/backer").mkdir(parents=True)
    (root / "installer").mkdir()
    (root / "android/app").mkdir(parents=True)
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nversion = "0.8.0"\n', encoding="utf-8")
    (root / "src/backer/_version.py").write_text('__version__ = "0.8.0"\n', encoding="utf-8")
    (root / "installer/backer-agent.iss").write_text('#define MyAppVersion "0.8.0"\n', encoding="utf-8")
    (root / "android/app/build.gradle.kts").write_text(
        '        versionCode = 800\n        versionName = "0.8.0"\n', encoding="utf-8"
    )


def test_bump_version_updates_every_release_version_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    write_release_files(tmp_path)
    bump_version = load_bump_version()
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["bump_version.py", "0.9.0"])

    assert bump_version.main() == 0
    assert 'version = "0.9.0"' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert '__version__ = "0.9.0"' in (tmp_path / "src/backer/_version.py").read_text(encoding="utf-8")
    assert 'MyAppVersion "0.9.0"' in (tmp_path / "installer/backer-agent.iss").read_text(encoding="utf-8")
    assert "versionCode = 900" in (tmp_path / "android/app/build.gradle.kts").read_text(encoding="utf-8")
    assert 'versionName = "0.9.0"' in (tmp_path / "android/app/build.gradle.kts").read_text(encoding="utf-8")


@pytest.mark.parametrize("version", ["0.9", "v0.9.0", "0.9.0-dev"])
def test_bump_version_rejects_invalid_versions_without_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version: str
) -> None:
    write_release_files(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    bump_version = load_bump_version()
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["bump_version.py", version])

    assert bump_version.main() == 2
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_bump_version_requires_changelog_heading_without_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_release_files(tmp_path, "## 0.8.0\n")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    bump_version = load_bump_version()
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["bump_version.py", "0.9.0"])

    assert bump_version.main() == 2
    assert "## 0.9.0" in capsys.readouterr().err
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
