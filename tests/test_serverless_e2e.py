"""Live serverless acceptance checks; network-backed cases opt in through CI env."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from backer.cli import main


def _environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config = config_dir / "config.yaml"
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("ProgramData", str(tmp_path / "programdata"))
    return config


def _add_and_run(runner: CliRunner, config: Path, source: Path, repository_type: str, location: list[str]) -> None:
    result = runner.invoke(
        main,
        [
            "--config",
            str(config),
            "repo",
            "add",
            "repository",
            "--init",
            "--type",
            repository_type,
            *location,
            "--passphrase-stdin",
            "--headless",
            "--yes",
        ],
        input="serverless-test-passphrase\n",
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        main,
        [
            "--config",
            str(config),
            "job",
            "create",
            "backup",
            "--repo",
            "repository",
            "--source",
            str(source),
            "--no-schedule",
            "--keep-last",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(main, ["--config", str(config), "job", "run", "backup", "--no-progress"])
    assert result.exit_code == 0, result.output


def _assert_lifecycle(runner: CliRunner, config: Path, source: Path, repository: Path | None) -> None:
    (source / "keep.txt").write_text("second", encoding="utf-8")
    (source / "deleted.txt").unlink()
    result = runner.invoke(main, ["--config", str(config), "job", "run", "backup", "--no-progress"])
    assert result.exit_code == 0, result.output
    snapshots = runner.invoke(main, ["--config", str(config), "snapshots", "--repo", "repository", "--json"])
    assert snapshots.exit_code == 0, snapshots.output
    assert len(json.loads(snapshots.output)) == 2
    if repository:
        sidecar = repository / ".backer" / "jobs" / "backup" / "config.json"
        assert json.loads(sidecar.read_text(encoding="utf-8"))["config"]["source_path"] == str(source)
    restored = source.parent / "restored"
    result = runner.invoke(
        main,
        [
            "--config",
            str(config),
            "restore",
            "--job",
            "backup",
            "--latest",
            "--destination",
            str(restored),
            "--into",
            "NEW",
            "--no-progress",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (restored / "keep.txt").read_text(encoding="utf-8") == "second"
    assert not (restored / "deleted.txt").exists()
    protected = source.parent / "protected"
    protected.mkdir()
    (protected / "do-not-delete.txt").write_text("keep", encoding="utf-8")
    result = runner.invoke(
        main,
        [
            "--config",
            str(config),
            "restore",
            "--job",
            "backup",
            "--snapshot",
            "missing-snapshot",
            "--destination",
            str(protected),
            "--into",
            "REPLACE",
            "--yes-replace",
            "--no-progress",
        ],
    )
    assert result.exit_code != 0
    assert (protected / "do-not-delete.txt").read_text(encoding="utf-8") == "keep"
    preview = runner.invoke(main, ["--config", str(config), "prune", "backup"])
    assert preview.exit_code == 0, preview.output
    verify = runner.invoke(main, ["--config", str(config), "verify", "backup"])
    assert verify.exit_code == 0, verify.output


def test_local_serverless_first_and_changed_backup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The local release leg must use the CLI, keystore, and real Kopia binary."""
    config = _environment(monkeypatch, tmp_path)
    source, repository = tmp_path / "source", tmp_path / "repository"
    source.mkdir()
    repository.mkdir()
    (source / "keep.txt").write_text("first", encoding="utf-8")
    (source / "deleted.txt").write_text("remove", encoding="utf-8")
    runner = CliRunner()

    _add_and_run(runner, config, source, "local", ["--path", str(repository)])
    _assert_lifecycle(runner, config, source, repository)


def test_s3_serverless_first_and_changed_backup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    names = (
        "BACKER_TEST_S3_ENDPOINT",
        "BACKER_TEST_S3_BUCKET",
        "BACKER_TEST_S3_ACCESS_KEY",
        "BACKER_TEST_S3_SECRET_KEY",
    )
    if not all(os.getenv(name) for name in names):
        pytest.skip("all BACKER_TEST_S3_* variables are required")
    endpoint, bucket, access_key, secret_key = (os.environ[name] for name in names)
    config = _environment(monkeypatch, tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "keep.txt").write_text("first", encoding="utf-8")
    (source / "deleted.txt").write_text("remove", encoding="utf-8")
    secret = tmp_path / "s3-secret"
    secret.write_text(secret_key, encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(config),
            "repo",
            "add",
            "repository",
            "--init",
            "--type",
            "s3",
            "--bucket",
            bucket,
            "--prefix",
            "serverless-e2e",
            "--endpoint",
            endpoint,
            "--region",
            "us-east-1",
            "--access-key-id",
            access_key,
            "--secret-key-file",
            str(secret),
            "--passphrase-stdin",
            "--headless",
            "--yes",
        ],
        input="serverless-test-passphrase\n",
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        main,
        [
            "--config",
            str(config),
            "job",
            "create",
            "backup",
            "--repo",
            "repository",
            "--source",
            str(source),
            "--no-schedule",
            "--keep-last",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(main, ["--config", str(config), "job", "run", "backup", "--no-progress"])
    assert result.exit_code == 0, result.output
    _assert_lifecycle(runner, config, source, None)
    from backer.serverless.s3_sidecar import S3Sidecar

    sidecar = S3Sidecar(
        {"bucket": bucket, "prefix": "serverless-e2e", "endpoint": endpoint, "region": "us-east-1"},
        {"access_key_id": access_key, "secret_access_key": secret_key},
    )
    document = sidecar.get(".backer/jobs/backup/config.json")
    assert document is not None
    assert json.loads(document)["config"]["source_path"] == str(source)


def _smb_environment() -> tuple[str, str, str, str] | None:
    names = ("BACKER_TEST_SMB_SERVER", "BACKER_TEST_SMB_SHARE", "BACKER_TEST_SMB_USERNAME", "BACKER_TEST_SMB_PASSWORD")
    values = tuple(os.getenv(name) for name in names)
    return values if all(values) else None


def _smb_repository_path() -> Path:
    value = os.getenv("BACKER_TEST_SMB_REPOSITORY_PATH")
    if not value:
        pytest.skip("BACKER_TEST_SMB_REPOSITORY_PATH is required")
    return Path(value) / "backer-test"


@pytest.mark.skipif(sys.platform == "win32", reason="Linux mount contract")
def test_smb_linux_serverless_mount_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    values = _smb_environment()
    if values is None:
        pytest.skip("all BACKER_TEST_SMB_* variables are required")
    server, share, username, password = values
    config = _environment(monkeypatch, tmp_path)
    repository = _smb_repository_path()
    source = tmp_path / "source"
    source.mkdir()
    (source / "keep.txt").write_text("first", encoding="utf-8")
    (source / "deleted.txt").write_text("remove", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(config),
            "repo",
            "add",
            "repository",
            "--init",
            "--type",
            "smb",
            "--host",
            server,
            "--share",
            share,
            "--path",
            "backer-test",
            "--username",
            username,
            "--password-stdin",
            "--headless",
            "--yes",
        ],
        input=f"{password}\nserverless-test-passphrase\n",
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        main,
        [
            "--config",
            str(config),
            "job",
            "create",
            "backup",
            "--repo",
            "repository",
            "--source",
            str(source),
            "--no-schedule",
            "--keep-last",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(main, ["--config", str(config), "job", "run", "backup", "--no-progress"])
    assert result.exit_code == 0, result.output
    _assert_lifecycle(runner, config, source, repository)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows redirector contract")
def test_smb_windows_real_redirector_and_1219(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    values = _smb_environment()
    if values is None:
        pytest.skip("all BACKER_TEST_SMB_* variables are required")
    server, share, username, password = values
    other = os.getenv("BACKER_TEST_SMB_OTHER_USERNAME")
    if not other:
        pytest.skip("BACKER_TEST_SMB_OTHER_USERNAME is required")
    from backer.core.mounts import SMBConnectionManager

    config = _environment(monkeypatch, tmp_path)
    repository = _smb_repository_path()
    source = tmp_path / "source"
    source.mkdir()
    (source / "keep.txt").write_text("first", encoding="utf-8")
    (source / "deleted.txt").write_text("remove", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(config),
            "repo",
            "add",
            "repository",
            "--init",
            "--type",
            "smb",
            "--host",
            server,
            "--share",
            share,
            "--path",
            "backer-test",
            "--username",
            username,
            "--password-stdin",
            "--headless",
            "--yes",
        ],
        input=f"{password}\nserverless-test-passphrase\n",
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        main,
        [
            "--config",
            str(config),
            "job",
            "create",
            "backup",
            "--repo",
            "repository",
            "--source",
            str(source),
            "--no-schedule",
            "--keep-last",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(main, ["--config", str(config), "job", "run", "backup", "--no-progress"])
    assert result.exit_code == 0, result.output
    _assert_lifecycle(runner, config, source, repository)

    manager = SMBConnectionManager()
    assert manager.connect_serverless(server, share, username, password)
    with pytest.raises(RuntimeError, match="SMB connection conflict"):
        manager.connect_serverless(server, share, other, password)
    assert manager._find_existing_connection(server)[1].casefold().endswith(username.casefold())
    assert manager.connect_serverless(server, share, other, password, is_system=True)
    assert manager._find_existing_connection(server)[1].casefold().endswith(other.casefold())
    manager.disconnect_serverless(server, share)
