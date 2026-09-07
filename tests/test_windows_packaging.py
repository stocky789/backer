from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]


def test_windows_builder_bundles_only_kopia() -> None:
    source = Path("scripts/build_agent.py").read_text()
    assert '["kopia"]' in source
    assert "restic" not in source.lower()
    assert "rclone" not in source.lower()


def test_frozen_service_entry_uses_its_install_directory_for_tools() -> None:
    source = (ROOT / "src/backer/agent/service_entry.py").read_text()

    assert 'if getattr(sys, "frozen", False):' in source
    assert 'os.environ.setdefault("BACKER_DATA_DIR", str(Path(sys.executable).resolve().parent))' in source
    assert source.index('os.environ.setdefault("BACKER_DATA_DIR"') < source.index("BackerAgent.from_config")


def test_installer_stops_both_agent_executables_and_cleans_up_task() -> None:
    source = (ROOT / "installer/backer-agent.iss").read_text()

    assert (
        "CloseApplicationsFilter=backer-desktop.exe,backer-agent-service.exe,backer.exe,backer-agent.exe"
        in source
    )
    assert "Exec('schtasks', '/end /tn BackerAgentService'" in source
    assert "Exec('taskkill', '/F /IM backer-agent-service.exe'" in source
    assert 'Parameters: "/end /tn BackerAgentService"' in source
    assert 'Parameters: "/delete /tn BackerAgentService /f"' in source
    assert 'Parameters: "/F /IM backer-agent-service.exe"' in source


def test_installer_removes_the_legacy_tk_agent_executable() -> None:
    source = (ROOT / "installer/backer-agent.iss").read_text()

    assert "[InstallDelete]" in source
    assert 'Type: files; Name: "{app}\\backer-agent.exe"' in source
    assert "Exec('taskkill', '/F /IM backer-agent.exe'" in source
    assert 'Parameters: "/F /IM backer-agent.exe"' in source


def test_scheduled_task_xml_preserves_spaced_service_path(monkeypatch) -> None:
    from backer.client import windows_service

    commands: list[list[str]] = []
    service_path = r"C:\Program Files\Backer Agent\backer-agent-service.exe"

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        commands.append(command)
        if command[1] == "/create":
            xml = Path(command[5]).read_text(encoding="utf-16")
            assert "<Command>C:\\Program Files\\Backer Agent\\backer-agent-service.exe</Command>" in xml
            assert "<Arguments></Arguments>" in xml
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(windows_service, "is_windows", lambda: True)
    monkeypatch.setattr(windows_service, "is_admin", lambda: True)
    monkeypatch.setattr(windows_service, "_prepare_service_config", lambda: None)
    monkeypatch.setattr(windows_service, "get_service_executable_path", lambda: service_path)
    monkeypatch.setattr(windows_service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(windows_service.subprocess, "run", fake_run)

    assert windows_service.create_background_scheduled_task()[0]
    assert ["schtasks", "/run", "/tn", "BackerAgentService"] in commands


def test_scheduled_task_fallback_quotes_spaced_service_path(monkeypatch) -> None:
    from backer.client import windows_service

    commands: list[list[str]] = []
    service_path = r"C:\Program Files\Backer Agent\backer-agent-service.exe"

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(windows_service, "get_service_executable_path", lambda: service_path)
    monkeypatch.setattr(windows_service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(windows_service.subprocess, "run", fake_run)

    assert windows_service._create_background_task_simple()[0]
    create_command = next(command for command in commands if command[1] == "/create")
    assert create_command[create_command.index("/tr") + 1] == f'"{service_path}"'


def test_service_config_honors_explicit_config_directory(monkeypatch, tmp_path: Path) -> None:
    from backer.client import windows_service

    source = tmp_path / "agent-config"
    source.mkdir()
    (source / "config.yaml").write_text("agent_id: agent-1\nrepositories: {}\njobs: {}\n")
    target = tmp_path / "program-data"
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(source))
    monkeypatch.setenv("ProgramData", str(target))
    monkeypatch.setattr(
        windows_service.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0)
    )

    windows_service._prepare_service_config()

    assert (target / "Backer" / "config.yaml").read_text() == "agent_id: agent-1\nrepositories: {}\njobs: {}\n"


def test_service_config_restricts_copied_credentials_to_system_and_administrators(monkeypatch, tmp_path: Path) -> None:
    from backer.client import windows_service

    source = tmp_path / "agent-config"
    source.mkdir()
    (source / "config.yaml").write_text("agent_id: agent-1\nrepositories: {}\njobs: {}\n")
    target = tmp_path / "program-data"
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        assert (target / "Backer" / "config.yaml").exists()
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("BACKER_CONFIG_DIR", str(source))
    monkeypatch.setenv("ProgramData", str(target))
    monkeypatch.setattr(windows_service.subprocess, "run", fake_run)

    windows_service._prepare_service_config()

    assert commands == [[
        "icacls", str(target / "Backer"), "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F",
        "/grant:r", "*S-1-5-32-544:(OI)(CI)F", "/remove:g", "*S-1-5-32-545", "/t", "/c",
    ]]


def test_service_config_fails_when_acl_hardening_fails(monkeypatch, tmp_path: Path) -> None:
    from backer.client import windows_service

    source = tmp_path / "agent-config"
    source.mkdir()
    (source / "config.yaml").write_text("agent_id: agent-1\nrepositories: {}\njobs: {}\n")
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(source))
    monkeypatch.setenv("ProgramData", str(tmp_path / "program-data"))
    monkeypatch.setattr(
        windows_service.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1)
    )

    with pytest.raises(OSError, match="restrict service config permissions"):
        windows_service._prepare_service_config()

    target = tmp_path / "program-data" / "Backer"
    assert not (target / "config.yaml").exists()
    assert not target.exists()
