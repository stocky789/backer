from pathlib import Path
from subprocess import CompletedProcess

import pytest
from click.testing import CliRunner

from backer.backends.base import BackupDestination
from backer.backends.kopia import KopiaBackend
from backer.cli import main


def test_probe_distinguishes_absent_unreachable_and_wrong_passphrase(monkeypatch) -> None:
    backend = KopiaBackend({"repository_password": "correct"})
    monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
    messages = [
        "repository not initialized in the provided storage",
        "cannot access storage path",
        "invalid repository password",
    ]

    def run(command, **_):
        if command[1:3] == ["repository", "disconnect"]:
            return CompletedProcess(command, 0, "", "")
        return CompletedProcess(command, 1, "", messages.pop(0))

    monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
    assert backend.repository_probe("missing")[0] == "absent"
    assert backend.repository_probe("offline")[0] == "unreachable"
    assert backend.repository_probe("wrong")[0] == "wrong_passphrase"


def test_serverless_connect_resets_connection_and_never_persists_credentials(monkeypatch) -> None:
    backend = KopiaBackend({"repository_password": "secret"})
    calls = []
    monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))

    def run(command, **_):
        calls.append(command)
        if command[1:3] == ["repository", "status"]:
            return CompletedProcess(command, 0, '{"uniqueIDHex":"abc"}', "")
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
    assert backend.repository_probe("repo") == ("present", "abc")
    assert calls[0][1:3] == ["repository", "disconnect"]
    assert "--no-persist-credentials" in calls[1]
    assert all("--use-credential-manager" not in call for call in calls)


def test_init_resets_and_disconnects_even_after_create_failure(monkeypatch, tmp_path: Path) -> None:
    backend = KopiaBackend({"repository_password": "secret"})
    calls = []
    monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))

    def run(command, **_):
        calls.append(command)
        if command[1:3] == ["repository", "create"]:
            return CompletedProcess(command, 1, "", "create failed")
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
    result = backend.init_repo(BackupDestination(str(tmp_path / "repo")))

    assert not result.success
    assert calls[0][1:3] == ["repository", "disconnect"]
    assert "--no-persist-credentials" in calls[1]
    assert calls[-1][1:3] == ["repository", "disconnect"]


@pytest.mark.parametrize("failure_at", ["connect", "status"])
def test_probe_preserves_kopia_error_text(monkeypatch, failure_at: str) -> None:
    backend = KopiaBackend({"repository_password": "secret"})
    monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))

    def run(command, **_):
        if command[1:3] == ["repository", "disconnect"]:
            return CompletedProcess(command, 0, "", "")
        if failure_at == "connect" or command[1:3] == ["repository", "status"]:
            return CompletedProcess(command, 1, "", "cannot access storage path: offline\n")
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)

    assert backend.repository_probe("offline")[0] == "unreachable"
    assert backend.last_repository_error == "cannot access storage path: offline\n"


def test_repo_attach_refuses_absent_without_create(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("backer.serverless.repositories.probe", lambda *_: ("absent", None, "nothing there"))
    created = []
    monkeypatch.setattr("backer.serverless.repositories.create", lambda *_: created.append(True))

    result = CliRunner().invoke(main, [
        "repo", "add", "Home", "--attach", "--path", str(tmp_path / "repo"), "--passphrase-stdin"
    ], input="secret\n")

    assert result.exit_code != 0
    assert created == []
    assert "nothing" in result.output.lower()


def test_repo_init_stores_only_verified_passphrase(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path / "data"))
    states = iter([("absent", None, ""), ("present", "unique", "")])
    monkeypatch.setattr("backer.serverless.repositories.probe", lambda *_: next(states))
    monkeypatch.setattr("backer.serverless.repositories.create", lambda *_: (True, ""))
    monkeypatch.setattr("backer.serverless.repositories.keystore.put", lambda *args, **_: "file")

    result = CliRunner().invoke(main, [
        "repo", "add", "Home", "--init", "--path", str(tmp_path / "repo"), "--passphrase-stdin"
    ], input="secret\n")

    assert result.exit_code == 0, result.output
    saved = (tmp_path / "config.yaml").read_text()
    assert "secret" not in saved
    assert "unique_id: unique" in saved


def test_repo_add_requires_headless_for_file_keystore(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("backer.serverless.repositories.file_fallback_required", lambda: True)

    result = CliRunner().invoke(main, [
        "repo", "add", "Home", "--attach", "--path", "repo", "--passphrase-stdin"
    ], input="secret\n")

    assert result.exit_code != 0
    assert "--headless" in result.output


def test_repository_config_keeps_s3_keys_out_of_config() -> None:
    from backer.core.config import RepositoryConfig

    record = RepositoryConfig(
        id="repo", name="Repo", type="s3", bucket="bucket", prefix="", endpoint="https://s3.example", region="us-east-1"
    )
    assert "access_key_id" not in record.model_dump(exclude_none=True)
