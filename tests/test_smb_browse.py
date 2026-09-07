"""SMB browsing must keep passwords out of process arguments."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from backer.core.smb_browse import SMBBrowser


def _ls_line(name: str, is_dir: bool, size: int = 0) -> str:
    attrs = "D" if is_dir else ""
    return f"  {name:<34}{attrs:<4}{size:>8}  Wed Dec  3 10:15:30 2025"


ROOT_LS = "\n".join(
    [
        _ls_line(".", True),
        _ls_line("..", True),
        _ls_line("Alpha", True),
        _ls_line("repo1", True),
        _ls_line("notes.txt", False, 12),
    ]
)
REPO_LS = "\n".join([_ls_line(".", True), _ls_line("kopia.repository", False, 37)])


def test_linux_list_directory_returns_dirs_only_with_repository_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from backer.core import smb_browse

    monkeypatch.setattr(smb_browse.sys, "platform", "linux")

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        script = command[-1]  # the smbclient -c command
        if script == "ls":
            return subprocess.CompletedProcess(command, 0, ROOT_LS, "")
        if 'cd "/repo1"' in script:
            return subprocess.CompletedProcess(command, 0, REPO_LS, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    success, entries = SMBBrowser.list_directory(
        "nas", "Backups", username="u", password="p", directories_only=True
    )

    assert success
    assert [(e.name, e.is_dir, e.is_repository) for e in entries] == [
        ("Alpha", True, False),
        ("repo1", True, True),
    ]


def test_linux_list_directory_includes_files_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from backer.core import smb_browse

    monkeypatch.setattr(smb_browse.sys, "platform", "linux")
    monkeypatch.setattr(
        subprocess, "run", lambda command, **_k: subprocess.CompletedProcess(command, 0, ROOT_LS, "")
    )

    success, entries = SMBBrowser.list_directory("nas", "Backups", username="u", password="p")

    assert success
    names = [(e.name, e.is_dir) for e in entries]
    assert ("notes.txt", False) in names  # files are listed for the default (browser) callers
    assert ("Alpha", True) in names


def test_linux_list_directory_maps_access_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    from backer.core import smb_browse

    monkeypatch.setattr(smb_browse.sys, "platform", "linux")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_k: subprocess.CompletedProcess(command, 1, "", "NT_STATUS_ACCESS_DENIED"),
    )

    assert SMBBrowser.list_directory("nas", "Backups", username="u", password="p") == (
        False,
        "Access denied to this directory",
    )


class _FakeUNC:
    """Minimal Path stand-in for the win32 iterdir/marker checks."""

    def __init__(self, name: str, children: dict[str, dict] | None = None, markers: set[str] | None = None):
        self.name = name
        self._children = children or {}
        self._markers = markers or set()

    def __truediv__(self, other: str) -> _FakeUNC:
        if other in self._children:
            return _FakeUNC(other, **self._children[other])
        # marker probe: exists() only true for declared markers
        node = _FakeUNC(other)
        node._exists = other in self._markers
        return node

    def iterdir(self):
        for child_name, spec in self._children.items():
            yield _FakeUNC(child_name, **spec)

    def is_dir(self) -> bool:
        return bool(self._children) or self.name in {"Alpha", "repo1"}

    def exists(self) -> bool:
        return getattr(self, "_exists", False)


def test_windows_list_directory_pins_argv_and_flags_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    from backer.core import smb_browse

    tree = {
        "Alpha": {"children": {}, "markers": set()},
        "repo1": {"children": {}, "markers": {"kopia.repository"}},
    }
    monkeypatch.setattr(smb_browse, "Path", lambda *_a: _FakeUNC("root", children=tree))

    commands = _win32_runs(monkeypatch, {})

    success, entries = SMBBrowser.list_directory(
        "nas", "backup", username="backup", password="sentinel-password", domain="CORP", directories_only=True
    )

    assert success
    assert [(e.name, e.is_repository) for e in entries] == [("Alpha", False), ("repo1", True)]
    assert commands == [
        ["net", "use", r"\\nas\IPC$", r"/user:CORP\backup", "*", "/persistent:no"],
        ["net", "use", r"\\nas\IPC$", "/delete", "/y"],
    ]
    assert all("sentinel-password" not in argument for command in commands for argument in command)


def test_windows_list_directory_maps_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _win32_runs(
        monkeypatch,
        {"use": subprocess.CompletedProcess(["net", "use"], 2, "", "System error 1326 has occurred.")},
    )

    assert SMBBrowser.list_directory("nas", "backup", username="u", password="p") == (
        False,
        "Login failed - invalid username or password",
    )


def test_cli_repo_browse_emits_contract_json(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from backer.cli import main

    def fake_list(server, share, path, username, password, domain, directories_only=False):
        assert password == "sentinel-password"
        assert directories_only is True
        from backer.core.smb_browse import DirectoryEntry

        return True, [
            DirectoryEntry(name="Alpha", is_dir=True),
            DirectoryEntry(name="repo1", is_dir=True, is_repository=True),
        ]

    monkeypatch.setattr("backer.core.smb_browse.SMBBrowser.list_directory", staticmethod(fake_list))

    result = CliRunner().invoke(
        main,
        ["repo", "browse", "--host", "nas", "--share", "backup", "--path", "/sub/", "--username", "u",
         "--password-stdin", "--json"],
        input="sentinel-password\n",
    )

    assert result.exit_code == 0, result.output
    import json as _json

    assert _json.loads(result.output) == {
        "path": "sub",
        "entries": [
            {"name": "Alpha", "is_dir": True, "is_repository": False},
            {"name": "repo1", "is_dir": True, "is_repository": True},
        ],
    }


def test_cli_repo_browse_maps_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from backer.cli import main

    monkeypatch.setattr(
        "backer.core.smb_browse.SMBBrowser.list_directory",
        staticmethod(lambda *a, **k: (False, "Login failed - invalid username or password")),
    )

    result = CliRunner().invoke(
        main,
        ["repo", "browse", "--host", "nas", "--share", "backup", "--username", "u", "--password-stdin"],
        input="pw\n",
    )

    assert result.exit_code != 0
    assert "Login failed" in result.output


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


def test_create_directory_keeps_password_off_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert SMBBrowser.make_directory("nas", "backup", "laptops/matt", "user", "sentinel-password")
    assert all("sentinel-password" not in argument for command in commands for argument in command)


NET_VIEW_OUTPUT = """Shared resources at \\\\nas


Share name  Type  Used as  Comment

-------------------------------------------------------------------------------
Backups     Disk           Nightly backup target
Media       Disk  Z:       Movies and TV
Public      Disk
Virtual Machines  Disk     Hyper-V exports
ADMIN$      Disk           Remote Admin
HPLaser     Print          Office printer
The command completed successfully.

"""


def _win32_runs(monkeypatch: pytest.MonkeyPatch, results: dict[str, subprocess.CompletedProcess[str]]):
    """Pretend to be Windows and record every argv, returning canned results per verb."""
    from backer.core import smb_browse

    commands: list[list[str]] = []
    monkeypatch.setattr(smb_browse.sys, "platform", "win32")

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return results.get(command[1], subprocess.CompletedProcess(command, 0, "", ""))

    monkeypatch.setattr(subprocess, "run", fake_run)
    return commands


def test_windows_list_shares_parses_net_view(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = _win32_runs(
        monkeypatch, {"view": subprocess.CompletedProcess(["net", "view"], 0, NET_VIEW_OUTPUT, "")}
    )

    success, shares = SMBBrowser.list_shares("nas", "backup", "sentinel-password", "CORP")

    assert success
    assert [(share.name, share.comment) for share in shares] == [
        ("Backups", "Nightly backup target"),
        ("Media", "Movies and TV"),
        ("Public", ""),
        # Share names can contain spaces; the columns, not the first token, delimit them.
        ("Virtual Machines", "Hyper-V exports"),
    ]
    assert commands == [
        ["net", "use", r"\\nas\IPC$", r"/user:CORP\backup", "*", "/persistent:no"],
        ["net", "view", r"\\nas"],
        ["net", "use", r"\\nas\IPC$", "/delete", "/y"],
    ]
    assert all("sentinel-password" not in argument for command in commands for argument in command)


def test_windows_list_shares_tears_down_session_when_net_view_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = _win32_runs(
        monkeypatch,
        {"view": subprocess.CompletedProcess(["net", "view"], 2, "", "System error 5 has occurred.")},
    )

    success, error = SMBBrowser.list_shares("nas", "backup", "sentinel-password")

    assert (success, error) == (False, "Access denied - check credentials")
    assert commands[-1] == ["net", "use", r"\\nas\IPC$", "/delete", "/y"]


def test_windows_list_shares_maps_connection_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = _win32_runs(
        monkeypatch,
        {
            "use": subprocess.CompletedProcess(
                ["net", "use"], 2, "", "System error 1326 has occurred.\nThe user name or password is incorrect."
            )
        },
    )

    success, error = SMBBrowser.list_shares("nas", "backup", "sentinel-password")

    assert (success, error) == (False, "Login failed - invalid username or password")
    # Nothing was created, so nothing is torn down and no enumeration is attempted.
    assert commands == [["net", "use", r"\\nas\IPC$", "/user:backup", "*", "/persistent:no"]]


def test_windows_error_mapping_covers_missing_server_and_conflict() -> None:
    from backer.core.smb_browse import windows_smb_error

    assert windows_smb_error("System error 53 has occurred.") == "Server not found or not accessible"
    assert "different credentials" in windows_smb_error("System error 1219 has occurred.")
    assert windows_smb_error("") == "Unknown error connecting to server"


def test_missing_host_smb_conf_gets_null_configfile(monkeypatch: pytest.MonkeyPatch) -> None:
    from backer.core import smb_browse

    monkeypatch.setattr(smb_browse.sys, "platform", "linux")
    monkeypatch.setattr(smb_browse.os.path, "exists", lambda path: False)
    command = smb_browse.smbclient_command("-L", "//nas")
    assert command[0] == "smbclient"
    assert "--configfile=/dev/null" in command

    monkeypatch.setattr(smb_browse.os.path, "exists", lambda path: True)
    assert "--configfile=/dev/null" not in smb_browse.smbclient_command("-L", "//nas")


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


def test_existing_connection_probe_reuses_without_credentials_and_removes_its_probe(
    monkeypatch, tmp_path: Path
) -> None:
    """The 1219 reuse action must not replace Explorer's credentials."""
    from backer.core import mounts
    from backer.core.mounts import SMBConnectionManager

    commands: list[list[str]] = []
    manager = SMBConnectionManager()
    monkeypatch.setattr(manager, "_find_existing_connection", lambda _server: (r"\\nas\media", "existing-user"))
    monkeypatch.setattr(mounts, "Path", lambda *_values: tmp_path)
    monkeypatch.setattr(
        mounts.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(command) or subprocess.CompletedProcess(command, 0, "", ""),
    )

    assert manager.connect_existing_serverless("nas", "backup", "folder")
    assert commands == [["net", "use", r"\\nas\backup", "/persistent:no"]]
    assert list(tmp_path.iterdir()) == []


def test_existing_connection_disconnect_targets_only_the_named_connection(monkeypatch) -> None:
    from backer.core import mounts
    from backer.core.mounts import SMBConnectionManager

    commands: list[list[str]] = []
    monkeypatch.setattr(
        mounts.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(command) or subprocess.CompletedProcess(command, 0, "", ""),
    )

    assert SMBConnectionManager().disconnect_existing_connection(r"\\nas\backup")
    assert commands == [["net", "use", r"\\nas\backup", "/delete", "/y"]]


def test_sanitize_removes_colon_and_slash_inline_suffix() -> None:
    module = _spike_module()

    assert module is not None
    assert "def/ghi" not in module.sanitize(":smb,pass=abc:def/ghi")


def test_comparison_path_requires_unc_or_cifs_mount() -> None:
    module = _spike_module()

    assert module is not None
    assert module.comparison_path(r"\\nas\share", is_dir=lambda: True, mount_source=lambda _path: None)
    assert module.comparison_path("/tmp/not-a-mount", is_dir=lambda: True, mount_source=lambda _path: None) is None
    assert module.comparison_path("/mnt/smb", is_dir=lambda: True, mount_source=lambda _path: "//nas/share")


def test_mount_source_requires_matching_cifs_share() -> None:
    module = _spike_module()

    assert module is not None
    mounts = ["/dev/sda1 / ext4 rw 0 0", "//nas/share /mnt/smb cifs rw 0 0"]
    assert module._mount_source(Path("/mnt/smb/backup"), mounts) == "//nas/share"
    assert module._matches_share("//nas/share", "nas", "share")
    assert not module._matches_share("//nas/other", "nas", "share")


def test_failure_observations_record_actual_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _spike_module()

    assert module is not None
    monkeypatch.setenv("SPIKE_SMB_DROP_COMMAND_JSON", '["drop-smb"]')
    calls: list[list[str]] = []

    def runner(argv, *_args, **_kwargs):
        calls.append(argv)
        failed = "--rclone-startup-timeout=1ms" in argv or argv[0] == "kopia"
        return subprocess.CompletedProcess(argv, int(failed), "secret obscured", "dropped secret")

    record: dict[str, object] = {}
    module.record_failure_observations(record, ":smb:share", "secret", "obscured", runner, {})

    assert record["failure_observations"]["rclone_startup_timeout"]["observed"] is True
    assert record["failure_observations"]["connection_drop"]["observed"] is True
    evidence = record["failure_observations"]["connection_drop"]["evidence"]
    assert evidence["returncode"] == 1
    assert evidence["stderr"] == "dropped [redacted]"
    assert "secret" not in str(record["failure_observations"])
    assert ["drop-smb"] in calls


def test_arm_d_runs_controlled_unc_baseline_and_records_ratio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _spike_module()

    assert module is not None
    record: dict[str, object] = {"elapsed_ms": {}, "repository_size": None}
    assert hasattr(module, "record_unc_baseline")
    monkeypatch.setenv("SPIKE_SMB_COMPARISON_PATH", str(tmp_path))
    monkeypatch.setattr(module, "_mount_source", lambda _path: "//nas/share")

    def runner(argv, *_args, **_kwargs):
        return subprocess.CompletedProcess(argv, 0, '[{"id":"snap"}]' if "list" in argv else "", "")

    record["elapsed_ms"] = {"snapshot_create": 10}
    module.record_unc_baseline(record, "nas", "share", tmp_path, runner)
    assert "unc_ratio" in record
    assert record["unc_within_1_25x"] is False
    assert record["failure_observations"] == {
        "rclone_startup_timeout": "unreachable: no controlled failure endpoint",
        "connection_drop": "unreachable: no controlled failure endpoint",
    }
