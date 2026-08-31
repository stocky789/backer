import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backer.core.config import BackerConfig, JobConfig, RepositoryConfig, SourceConfig


def test_sidecar_job_config_uses_stable_subfolder_and_rejects_secret(tmp_path: Path) -> None:
    from backer.serverless.sidecar import save_job_config

    config = {"source_path": "C:/Users/me/Documents", "repository_hint": {"type": "local", "path": "repo"}}
    path = save_job_config(tmp_path, "Nightly: Documents", "agent-one", config, [])

    assert path == tmp_path / ".backer" / "jobs" / "Nightly_ Documents" / "config.json"
    saved = json.loads(path.read_text())
    assert saved["owner_agent_id"] == "agent-one"
    assert saved["config"] == config
    with pytest.raises(ValueError, match="secret"):
        save_job_config(tmp_path, "Other", "agent-one", {"password": "nope"}, [])


def test_adopt_preserves_job_name_and_never_rewrites_owner_sidecar(tmp_path: Path) -> None:
    from backer.serverless.sidecar import adopt_jobs, save_job_config

    save_job_config(tmp_path, "Nightly", "old-agent", {"source_path": "/missing", "excludes": ["*.tmp"]}, [])
    config = BackerConfig(
        agent_id="new-agent", repositories={"repo": RepositoryConfig(name="Repo", type="local", path=str(tmp_path))}
    )

    adopted = adopt_jobs(config, "repo", tmp_path, ["Nightly"], source_paths={"Nightly": "/local"})

    assert adopted == ["Nightly"]
    assert config.jobs["Nightly"] == JobConfig(
        repository="repo", source=SourceConfig(path="/local", excludes=["*.tmp"])
    )
    assert (
        json.loads((tmp_path / ".backer" / "jobs" / "Nightly" / "config.json").read_text())["owner_agent_id"]
        == "old-agent"
    )


def test_due_jobs_records_fire_before_backup_starts(tmp_path: Path) -> None:
    from backer.serverless.schedule import due_jobs

    config = BackerConfig(
        jobs={
            "nightly": JobConfig(repository="repo", source=SourceConfig(path="/data"), schedule={"cron": "0 * * * *"})
        }
    )
    now = datetime(2026, 9, 1, 2, 1, tzinfo=UTC)

    assert due_jobs(config, now, tmp_path) == ["nightly"]
    assert due_jobs(config, now, tmp_path) == []
    assert json.loads((tmp_path / "schedule.json").read_text())["nightly"] == "2026-09-01T02:01:00Z"


def test_serverless_smb_system_reclaims_1219_without_cmdkey(monkeypatch) -> None:
    from backer.core.mounts import SMBConnectionManager

    calls: list[list[str]] = []
    attempts = 0

    def run(command: list[str], **kwargs: object):
        nonlocal attempts
        calls.append(command)
        if command == ["net", "use"]:
            return type("Result", (), {"returncode": 0, "stdout": "OK \\\\nas\\share", "stderr": ""})()
        if command == ["net", "use", "\\\\nas\\share"]:
            return type("Result", (), {"returncode": 0, "stdout": "User name: other-user", "stderr": ""})()
        if command[0:3] == ["net", "use", "\\\\nas\\share"] and "*" in command:
            attempts += 1
            return type(
                "Result", (), {"returncode": 1 if attempts == 1 else 0, "stdout": "", "stderr": "System error 1219"}
            )()
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("backer.core.mounts.subprocess.run", run)
    manager = SMBConnectionManager()

    assert manager.connect_serverless("nas", "share", "backup", "secret", is_system=True)
    assert ["net", "use", "\\\\nas\\share", "/delete", "/y"] in calls
    assert not any(command[0] == "cmdkey" for command in calls)


def test_rescope_repository_secrets_to_machine(monkeypatch) -> None:
    from backer.serverless.repositories import rescope_secrets_for_system

    values = {("pass", False): "passphrase", ("store", False): '{"key":"value"}'}
    writes: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        "backer.serverless.repositories.keystore.get",
        lambda key, *, machine_scope=False: values.get((key, machine_scope)),
    )

    def put(key: str, value: str, *, machine_scope: bool = False) -> str:
        writes.append((key, value, machine_scope))
        values[key, machine_scope] = value
        return "file"

    monkeypatch.setattr("backer.serverless.repositories.keystore.put", put)
    config = BackerConfig(
        repositories={
            "repo": RepositoryConfig(
                name="Repo", type="local", path="/repo", passphrase_ref="pass", storage_password_ref="store"
            )
        }
    )

    rescope_secrets_for_system(config)

    assert writes == [("pass", "passphrase", True), ("store", '{"key":"value"}', True)]
    assert config.repositories["repo"].scope == "machine"


def test_due_lock_is_nonblocking(tmp_path: Path) -> None:
    from backer.serverless.schedule import run_lock

    with run_lock(tmp_path) as first:
        assert first
        with run_lock(tmp_path) as second:
            assert not second


def test_sidecar_hint_drops_absolute_destination_path(tmp_path: Path) -> None:
    from backer.serverless.sidecar import save_job_config

    path = save_job_config(
        tmp_path,
        "Nightly",
        "agent",
        {"source_path": "C:/data", "repository_hint": {"type": "local", "path": "C:/repo"}},
        [],
    )

    assert "path" not in json.loads(path.read_text())["config"]["repository_hint"]
