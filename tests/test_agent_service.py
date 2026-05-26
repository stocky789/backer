from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backer.agent import service as agent_service
from backer.agent.service import AgentService, SMBConnectionManager


def make_agent(tmp_path: Path) -> AgentService:
    return AgentService(
        server_url="http://localhost:8420",
        client_id="test-agent",
        client_secret="secret",
        tools_dir=tmp_path / "tools",
    )


def test_smb_connection_uses_explicit_credentials_when_cmdkey_fails(monkeypatch) -> None:
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        commands.append(cmd)
        result = MagicMock()
        result.stdout = ""
        result.stderr = ""
        result.returncode = 1 if cmd[:2] == ["cmdkey", "/add"] else 0
        return result

    monkeypatch.setattr(agent_service.subprocess, "run", fake_run)

    manager = SMBConnectionManager()

    assert manager.connect("192.168.0.254", "backer", "matt", "secret", "truenas")

    assert ["net", "use", "\\\\192.168.0.254\\backer", "/user:truenas\\matt", "secret"] in commands
    assert ["net", "use", "\\\\192.168.0.254\\backer", "/persistent:no"] not in commands


def test_backup_retry_treats_false_backup_result_as_failure(tmp_path: Path, monkeypatch) -> None:
    agent = make_agent(tmp_path)
    monkeypatch.setattr(agent, "_execute_backup", lambda payload: False)

    with pytest.raises(RuntimeError, match="Backup failed: gitprojects"):
        agent._execute_backup_with_retry({"job_name": "gitprojects"}, max_retries=1)


def test_windows_unc_repository_parent_is_created_before_repo_init(tmp_path: Path, monkeypatch) -> None:
    agent = make_agent(tmp_path)
    created_paths: list[str] = []

    def fake_mkdir(self: Path, parents: bool = False, exist_ok: bool = False) -> None:
        created_paths.append(str(self))
        assert parents is True
        assert exist_ok is True

    monkeypatch.setattr(agent_service.sys, "platform", "win32")
    monkeypatch.setattr(agent_service.Path, "mkdir", fake_mkdir)

    agent._ensure_repository_parent_directory("\\\\192.168.0.254\\backer\\Agents\\gitprojects")

    assert created_paths == ["\\\\192.168.0.254\\backer\\Agents"]
