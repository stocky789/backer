"""Release version bump checks."""

from __future__ import annotations

import importlib.util
import stat
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


def release_paths(root: Path) -> list[Path]:
    return [
        root / "pyproject.toml",
        root / "src/backer/_version.py",
        root / "installer/backer-agent.iss",
        root / "android/app/build.gradle.kts",
    ]


def transaction_artifacts(root: Path) -> list[Path]:
    return list(root.glob(".backer-bump-version*"))


def test_bump_version_preserves_crlf_bytes_and_unchanged_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_release_files(tmp_path, "## 0.9.1\r\n")
    for path in release_paths(tmp_path):
        path.write_bytes(
            path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n") + b"# unchanged\r\n"
        )
    before = {path: path.read_bytes() for path in release_paths(tmp_path)}
    bump_version = load_bump_version()
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["bump_version.py", "0.9.1"])

    assert bump_version.main() == 0
    for path, original in before.items():
        updated = path.read_bytes()
        assert updated.count(b"\r\n") == original.count(b"\r\n")
        assert updated.replace(b"0.9.1", b"0.8.0").replace(b"901", b"800") == original
    assert not transaction_artifacts(tmp_path)


def test_bump_version_rolls_back_each_target_replace_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_release_files(tmp_path)
    paths = release_paths(tmp_path)
    bump_version = load_bump_version()
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)

    for position in range(len(paths)):
        before = {path: path.read_bytes() for path in paths}
        real_replace = bump_version.os.replace
        target_replaces = 0

        def fail_one_target_replace(source: Path | str, destination: Path | str) -> None:
            nonlocal target_replaces
            if Path(destination) in paths:
                target_replaces += 1
                if target_replaces == position + 1:
                    raise OSError("replace failed")
            real_replace(source, destination)

        monkeypatch.setattr(bump_version.os, "replace", fail_one_target_replace)
        monkeypatch.setattr(sys, "argv", ["bump_version.py", "0.9.0"])
        assert bump_version.main() == 1
        assert {path: path.read_bytes() for path in paths} == before
        assert not transaction_artifacts(tmp_path)
        monkeypatch.setattr(bump_version.os, "replace", real_replace)


def test_bump_version_cleans_up_after_staged_write_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_release_files(tmp_path)
    paths = release_paths(tmp_path)
    before = {path: path.read_bytes() for path in paths}
    bump_version = load_bump_version()
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    real_write = bump_version.write_durable

    def fail_staged_write(path: Path, data: bytes, *_: object) -> None:
        if path.name.endswith(".new"):
            raise OSError("staged write failed")
        real_write(path, data, *_)

    monkeypatch.setattr(bump_version, "write_durable", fail_staged_write)
    monkeypatch.setattr(sys, "argv", ["bump_version.py", "0.9.0"])

    assert bump_version.main() == 1
    assert {path: path.read_bytes() for path in paths} == before
    assert not transaction_artifacts(tmp_path)


def test_bump_version_preserves_target_modes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    write_release_files(tmp_path)
    paths = release_paths(tmp_path)
    for path in paths:
        path.chmod(0o600)
    before_modes = {path: stat.S_IMODE(path.stat().st_mode) for path in paths}
    bump_version = load_bump_version()
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["bump_version.py", "0.9.0"])

    assert bump_version.main() == 0
    assert {path: stat.S_IMODE(path.stat().st_mode) for path in paths} == before_modes


@pytest.mark.parametrize("failure_kind", ["write", "replace"])
def test_bump_version_recovers_after_rollback_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure_kind: str, capsys: pytest.CaptureFixture[str]
) -> None:
    write_release_files(tmp_path)
    paths = release_paths(tmp_path)
    before = {path: path.read_bytes() for path in paths}
    bump_version = load_bump_version()
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    real_replace = bump_version.os.replace
    target_replaces = 0

    def fail_target_then_rollback(source: Path | str, destination: Path | str) -> None:
        nonlocal target_replaces
        if Path(destination) in paths:
            target_replaces += 1
            if target_replaces == 2 or (target_replaces > 2 and failure_kind == "replace"):
                raise OSError("replace failed")
        real_replace(source, destination)

    monkeypatch.setattr(bump_version.os, "replace", fail_target_then_rollback)
    if failure_kind == "write":
        real_write = bump_version.write_durable

        def fail_restore_write(path: Path, data: bytes, *_: object) -> None:
            if path.name.endswith(".restore"):
                raise OSError("restore write failed")
            real_write(path, data, *_)

        monkeypatch.setattr(bump_version, "write_durable", fail_restore_write)
    monkeypatch.setattr(sys, "argv", ["bump_version.py", "0.9.0"])

    assert bump_version.main() == 1
    error = capsys.readouterr().err
    assert "replace failed" in error
    assert "rollback failed" in error
    assert transaction_artifacts(tmp_path)
    monkeypatch.undo()
    bump_version = load_bump_version()
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["bump_version.py", "bad"])
    assert bump_version.main() == 2
    assert {path: path.read_bytes() for path in paths} == before
    assert not transaction_artifacts(tmp_path)


def test_bump_version_recovers_an_interrupted_transaction_before_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_release_files(tmp_path)
    paths = release_paths(tmp_path)
    before = {path: path.read_bytes() for path in paths}
    bump_version = load_bump_version()
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    real_replace = bump_version.os.replace
    target_replaces = 0

    def interrupt_second_target_replace(source: Path | str, destination: Path | str) -> None:
        nonlocal target_replaces
        if Path(destination) in paths:
            target_replaces += 1
            if target_replaces == 2:
                raise KeyboardInterrupt
        real_replace(source, destination)

    monkeypatch.setattr(bump_version.os, "replace", interrupt_second_target_replace)
    monkeypatch.setattr(sys, "argv", ["bump_version.py", "0.9.0"])
    with pytest.raises(KeyboardInterrupt):
        bump_version.main()
    assert transaction_artifacts(tmp_path)
    monkeypatch.undo()
    bump_version = load_bump_version()
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["bump_version.py", "invalid"])

    assert bump_version.main() == 2
    assert {path: path.read_bytes() for path in paths} == before
    assert not transaction_artifacts(tmp_path)
