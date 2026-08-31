import os
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from backer.client.agent import BackerAgent
from backer.core import paths
from backer.core.config import BackerConfig, RepositoryConfig, load_config


def _server() -> dict[str, str]:
    return {"server_url": "https://backer.example", "client_id": "agent-1", "client_secret": "secret"}


def test_agent_yaml_and_gui_json_merge_into_one_config(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "appdata" / "Backer"
    config_dir.mkdir(parents=True)
    (config_dir / "agent.yaml").write_text("server_url: https://agent\nclient_id: agent-1\nclient_secret: secret\n")
    (config_dir / "config.json").write_text('{"server_url": "https://gui", "client_id": "gui-1", "hostname": "old"}')
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(config_dir))

    config = load_config()

    assert config.agent_id == "agent-1"
    assert config.server.model_dump(exclude_none=True) == _server() | {
        "server_url": "https://agent", "heartbeat_interval": 60
    }
    assert (config_dir / "config.yaml").exists()
    assert (config_dir / "agent.yaml").exists()
    assert (config_dir / "config.json").exists()


def test_migration_finds_legacy_config_in_program_data(monkeypatch, tmp_path: Path) -> None:
    appdata, program_data = tmp_path / "appdata", tmp_path / "program-data"
    legacy = program_data / "Backer"
    legacy.mkdir(parents=True)
    (legacy / "agent.yaml").write_text("server_url: https://server\nclient_id: system\nclient_secret: secret\n")
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("ProgramData", str(program_data))
    monkeypatch.delenv("BACKER_CONFIG_DIR", raising=False)

    config = load_config()

    assert config.agent_id == "system"
    assert (appdata / "Backer" / "config.yaml").exists()


def test_unprivileged_linux_config_dir_is_xdg(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("BACKER_CONFIG_DIR", raising=False)
    monkeypatch.setattr(paths, "_machine_config_dir", lambda: tmp_path / "etc" / "backer")

    assert paths.get_config_dir() == tmp_path / "xdg" / "backer"


def test_etc_backer_is_used_when_it_holds_the_only_config(monkeypatch, tmp_path: Path) -> None:
    machine = tmp_path / "etc" / "backer"
    machine.mkdir(parents=True)
    (machine / "config.yaml").write_text("agent_id: only\nrepositories: {}\njobs: {}\n")
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("BACKER_CONFIG_DIR", raising=False)
    monkeypatch.setattr(paths, "_machine_config_dir", lambda: machine)

    assert paths.get_config_dir() == machine


def test_data_dir_honours_backer_data_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path / "data"))

    assert paths.get_data_dir() == tmp_path / "data"


def test_agent_uninstall_preserves_a_server_data_directory(tmp_path: Path) -> None:
    from backer import cli

    (tmp_path / "backer.db").write_text("server")

    assert cli._is_server_data_dir(tmp_path)


def test_repository_options_reject_inline_secrets() -> None:
    with pytest.raises(ValidationError):
        RepositoryConfig.model_validate({
            "name": "repo",
            "type": "s3",
            "repository_options": {"secret_access_key": "plaintext"},
        })


def test_repository_options_reject_nested_inline_secrets() -> None:
    with pytest.raises(ValidationError):
        RepositoryConfig.model_validate({
            "name": "repo",
            "type": "s3",
            "repository_options": {"nested": [{"password": "plaintext"}]},
        })


@pytest.mark.parametrize("secret_key", ["credential", "private_key"])
def test_repository_options_cannot_round_trip_arbitrary_secret_values(secret_key: str, tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "agent_id: agent\nrepositories:\n  repo:\n    name: Repo\n    type: local\n"
        f"    repository_options: {{{secret_key}: plaintext}}\njobs: {{}}\n"
    )

    with pytest.raises(ValidationError):
        BackerConfig.load(path)


def test_invalid_model_config_names_the_resolved_path(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("agent_id: agent\nrepositories: {}\njobs: {}\nfuture: true\n")
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))

    with pytest.raises(ValidationError, match=re.escape(str(path))):
        load_config()


def test_uninstall_preserves_colocated_server_config_and_data(monkeypatch, tmp_path: Path) -> None:
    import shutil

    from backer import cli

    (tmp_path / "backer.db").write_text("server")
    removals: list[Path] = []
    monkeypatch.setattr("backer.client.agent.get_config_dir", lambda: tmp_path)
    monkeypatch.setattr("backer.client.agent.get_data_dir", lambda: tmp_path)
    monkeypatch.setattr("backer.client.windows_service.is_windows", lambda: False)
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(shutil, "rmtree", lambda path, **_kwargs: removals.append(path))

    cli.agent_uninstall.callback(keep_config=False, yes=True)

    assert removals == []
    assert (tmp_path / "backer.db").exists()


def test_save_is_atomic_and_private(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"

    BackerConfig(agent_id="agent-1").save(path)

    assert not list(tmp_path.glob("config.yaml.*.tmp"))
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("field", ["backend", "backend_type", "backend_options"])
def test_repository_config_rejects_engine_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        RepositoryConfig.model_validate({"name": "repo", "type": "local", "path": "/repo", field: "kopia"})


def test_client_agent_reexports_config_dir() -> None:
    from backer.client.agent import get_config_dir

    assert get_config_dir is paths.get_config_dir


def test_from_config_reads_unified_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    BackerConfig(agent_id="agent-1", server=_server()).save(tmp_path / "config.yaml")

    agent = BackerAgent.from_config()

    assert agent.server_url == "https://backer.example"
    assert agent.config_path == tmp_path / "config.yaml"


def test_from_config_falls_back_to_agent_yaml(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    (tmp_path / "agent.yaml").write_text("server_url: https://agent\nclient_id: agent-1\nclient_secret: secret\n")

    assert BackerAgent.from_config().client_id == "agent-1"
    (tmp_path / "agent.yaml").unlink()
    with pytest.raises(FileNotFoundError, match="Agent config not found"):
        BackerAgent.from_config()


def test_agent_setup_preserves_serverless_config(monkeypatch, tmp_path: Path) -> None:
    from backer.cli import agent_setup

    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    config = BackerConfig(
        agent_id="agent-1",
        server=_server(),
        repositories={"repo": {"name": "Repo", "type": "local", "path": "/repo"}},
        jobs={"job": {"repository": "repo", "source": {"path": "/source"}}},
    )
    config.save(tmp_path / "config.yaml")
    monkeypatch.setattr("backer.client.setup_wizard.run_wizard", lambda: True)

    agent_setup.callback()

    saved = BackerConfig.load(tmp_path / "config.yaml")
    assert saved.server is None
    assert set(saved.repositories) == {"repo"}
    assert set(saved.jobs) == {"job"}


def test_invalid_config_raises_instead_of_defaulting(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    contents = "agent_id: [broken\n"
    path.write_text(contents)
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))

    with pytest.raises(ValueError, match=re.escape(str(path))):
        load_config()

    assert path.read_text() == contents


def test_server_managed_and_serverless_coexist(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    config = BackerConfig(
        agent_id="agent-1",
        server=_server(),
        repositories={"repo": {"name": "Repo", "type": "local", "path": "/repo"}},
        jobs={"job": {"repository": "repo", "source": {"path": "/source"}}},
    )
    config.save(path)

    assert BackerConfig.load(path) == config


def test_migration_runs_once(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    (tmp_path / "agent.yaml").write_text("server_url: https://server\nclient_id: agent-1\nclient_secret: secret\n")

    load_config()
    path = tmp_path / "config.yaml"
    first = (path.read_bytes(), path.stat().st_mtime_ns)
    load_config()

    assert (path.read_bytes(), path.stat().st_mtime_ns) == first


def test_conflicting_legacy_pairs_do_not_merge(monkeypatch, caplog, tmp_path: Path) -> None:
    appdata, program_data = tmp_path / "appdata", tmp_path / "program-data"
    for base, client_id in ((appdata, "user"), (program_data, "system")):
        directory = base / "Backer"
        directory.mkdir(parents=True)
        (directory / "agent.yaml").write_text(
            f"server_url: https://{client_id}\nclient_id: {client_id}\nclient_secret: secret\n"
        )
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("ProgramData", str(program_data))
    monkeypatch.delenv("BACKER_CONFIG_DIR", raising=False)

    config = load_config()

    assert config.agent_id == "user"
    assert "system" in caplog.text


def test_example_config_round_trips(tmp_path: Path) -> None:
    source = Path(__file__).parent / "fixtures" / "config_example.yaml"
    path = tmp_path / "config.yaml"
    path.write_bytes(source.read_bytes())

    config = BackerConfig.load(path)
    config.save(path)

    assert BackerConfig.load(path) == config


@pytest.mark.parametrize("field", ["backend", "backend_type", "backend_options"])
def test_no_engine_field_survives_a_round_trip(field: str, tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"agent_id: agent\nrepositories: {{}}\njobs:\n  job:\n    repository: repo\n"
        f"    source: {{path: /source}}\n    {field}: kopia\n"
    )

    with pytest.raises(ValidationError):
        BackerConfig.load(path)


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("agent_id: agent\nrepositories: {}\njobs: {}\nfuture: true\n")

    with pytest.raises(ValidationError):
        BackerConfig.load(path)
