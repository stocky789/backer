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


def test_spike_argv_guard_rejects_inline_secret_without_matching_environment() -> None:
    module = _spike_module()

    assert module is not None
    with pytest.raises(module.ArgvLeakError):
        module.assert_argv_safe(["kopia", "--remote-path=:smb,pass=other-secret:/share"], None)


def test_spike_sanitizes_slash_secret_and_error_text() -> None:
    module = _spike_module()

    assert module is not None
    assert hasattr(module, "sanitize")
    secret = "secret/with/slash"
    value = module.sanitize(f":smb,pass={secret}: and {secret}", secret, "encoded-secret")
    assert secret not in value
    assert "encoded-secret" not in module.sanitize("encoded-secret", secret, "encoded-secret")


def test_arm_d_runs_full_lifecycle_and_inspects_config(tmp_path: Path) -> None:
    module = _spike_module()

    assert module is not None
    assert hasattr(module, "run_arm_d_workload")
    calls: list[list[str]] = []

    def runner(argv, *_args, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, '[{"id":"snap"}]' if "list" in argv else "", "")

    config = tmp_path / "repository.config"
    config.write_text("safe config", encoding="utf-8")
    record: dict[str, object] = {"elapsed_ms": {}, "repository_size": None}
    module.run_arm_d_workload(
        record, "nas", "share", "user", "secret", "obscured", runner, tmp_path, config, workload_bytes=1, file_count=1
    )

    assert [command[1:3] for command in calls] == [
        ["repository", "create"],
        ["repository", "connect"],
        ["snapshot", "create"],
        ["snapshot", "list"],
        ["snapshot", "restore"],
        ["snapshot", "verify"],
        ["snapshot", "expire"],
    ]


def test_safe_manager_path_keeps_password_off_nested_argv() -> None:
    from backer.core.mounts import SMBConnectionManager

    assert hasattr(SMBConnectionManager, "connect_with_stdin")
    commands: list[list[str]] = []

    def runner(argv, **_kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert SMBConnectionManager().connect_with_stdin("nas", "share", "user", "sentinel-password", runner)
    assert all("sentinel-password" not in value for command in commands for value in command)


def test_sanitize_removes_colon_and_slash_inline_suffix() -> None:
    module = _spike_module()

    assert module is not None
    assert "def/ghi" not in module.sanitize(":smb,pass=abc:def/ghi")


def test_arm_d_runs_controlled_unc_baseline_and_records_ratio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _spike_module()

    assert module is not None
    record: dict[str, object] = {"elapsed_ms": {}, "repository_size": None}
    assert hasattr(module, "record_unc_baseline")
    monkeypatch.setenv("SPIKE_SMB_COMPARISON_PATH", str(tmp_path))

    def runner(argv, *_args, **_kwargs):
        return subprocess.CompletedProcess(argv, 0, '[{"id":"snap"}]' if "list" in argv else "", "")

    record["elapsed_ms"] = {"snapshot_create": 10}
    module.record_unc_baseline(record, "nas", "share", tmp_path, runner)
    assert "unc_ratio" in record
    assert record["unc_within_1_25x"] is False
    assert record["failure_observations"] == {
        "rclone_startup_timeout": "not observed",
        "connection_drop": "not observed",
    }
