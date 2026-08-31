"""SMB browsing must keep passwords out of process arguments."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from backer.core.smb_browse import SMBBrowser


def test_no_password_on_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "Disk|Backups|", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    SMBBrowser.list_shares("nas", "backup", "sentinel-password")
    SMBBrowser.list_directory("nas", "Backups", username="backup", password="sentinel-password")

    assert commands
    assert all("sentinel-password" not in argument for command in commands for argument in command)


def _spike_module():
    script = Path(__file__).parents[1] / "scripts" / "spike_smb_discovery.py"
    if not script.exists():
        return None
    spec = importlib.util.spec_from_file_location("spike_smb_discovery", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_spike_argv_guard_rejects_plaintext_and_obscured_password() -> None:
    module = _spike_module()

    assert module is not None
    with pytest.raises(module.ArgvLeakError):
        module.assert_argv_safe(
            ["kopia", "--remote-path=:smb,pass=sentinel-password:"], "sentinel-password", "obscured"
        )
    with pytest.raises(module.ArgvLeakError):
        module.assert_argv_safe(["kopia", "--remote-path=:smb,pass=obscured:"], "sentinel-password", "obscured")


def test_spike_record_redacts_inline_password() -> None:
    module = _spike_module()

    assert module is not None
    record = module._base_record("d", "nas", ":smb,host=nas,pass=sentinel-password:/share", "nas", "3.1.1")
    assert "sentinel-password" not in record["share"]
