from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backer.agent.service import AgentService
from backer.backends.base import BackupDestination, BackupSource
from backer.backends.restic import ResticBackend
from backer.server.app import _build_backup_command_payload, create_app
from backer.server.s3 import S3ConfigError, parse_s3_config, restic_s3_config
from backer.server.web.auth import get_setup_token
from backer.tools.manager import ToolManager


def config(**overrides: object) -> dict[str, object]:
    return {
        "bucket": "backer-test",
        "prefix": "agents/host-one",
        "endpoint": "https://minio.example.test:9000",
        "region": "us-east-1",
        "access_key_id": "test-access-key",
        "secret_access_key": "test-secret-key",
        "use_path_style": True,
        **overrides,
    }


def test_s3_config_builds_restricted_restic_boundary() -> None:
    result = restic_s3_config(config())

    assert result["repository"] == "s3:https://minio.example.test:9000/backer-test/agents/host-one"
    assert result["options"] == ["-o", "s3.region=us-east-1", "-o", "s3.bucket-lookup=path"]
    assert result["environment"] == {
        "AWS_ACCESS_KEY_ID": "test-access-key",
        "AWS_SECRET_ACCESS_KEY": "test-secret-key",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
    assert "secret_access_key" not in result["public_config"]
    assert "access_key_id" not in result["public_config"]


def test_restic_s3_options_and_credentials_apply_to_every_command(monkeypatch) -> None:
    backend = ResticBackend({"s3": config()})
    monkeypatch.setattr(backend, "_get_binary", lambda: "restic")

    command = backend._build_backup_command("s3:https://minio.example.test:9000/backer-test", "/source")

    assert command == [
        "restic", "backup", "--repo", "s3:https://minio.example.test:9000/backer-test",
        "-o", "s3.region=us-east-1", "-o", "s3.bucket-lookup=path", "--json", "/source",
    ]
    assert backend._env["AWS_ACCESS_KEY_ID"] == "test-access-key"
    assert backend._env["AWS_SECRET_ACCESS_KEY"] == "test-secret-key"


def test_s3_connection_reports_provider_errors_without_exposing_credentials(monkeypatch) -> None:
    backend = ResticBackend({"s3": config(), "repository_password": "repo-password"})
    monkeypatch.setattr(backend, "_get_binary", lambda: "restic")
    monkeypatch.setattr(
        "backer.backends.restic.subprocess.run",
        lambda *_, **__: subprocess.CompletedProcess([], 1, "", "AccessDenied"),
    )

    success, message = backend.test_connection(BackupDestination("s3:https://minio.example.test:9000/backer-test"))

    assert not success
    assert message == "AccessDenied"


def test_agent_logs_redact_both_s3_credentials(tmp_path) -> None:
    safe = AgentService("http://server", "agent", "secret", tools_dir=tmp_path)._redact_sensitive_data({"s3": config()})

    assert safe["s3"]["access_key_id"] == "***REDACTED***"
    assert safe["s3"]["secret_access_key"] == "***REDACTED***"


def test_s3_api_encrypts_credentials_and_builds_restic_agent_payload(tmp_path, monkeypatch) -> None:
    app = create_app(tmp_path)
    monkeypatch.setattr(ResticBackend, "test_connection", lambda *_: (True, "mocked S3 connection"))
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/setup", data={
            "username": "owner", "display_name": "Owner", "password": "test-admin-password",
            "confirm_password": "test-admin-password", "setup_token": get_setup_token(),
            "timezone": "Australia/Sydney", "public_url": "https://backer.example.test",
        })
        response = client.post("/api/v1/repositories", json={
            "name": "Offsite", "type": "s3", "backend": "restic",
            "repository_password": "repo-password", "s3": config(),
        })
    assert response.status_code == 200
    repo_id = response.json()["id"]
    storage = app.state.storage
    repo = storage.get_repository(repo_id)
    assert repo is not None
    assert repo["config"] == {
        "backend_type": "restic",
        "s3": {
            "bucket": "backer-test", "prefix": "agents/host-one",
            "endpoint": "https://minio.example.test:9000", "region": "us-east-1", "use_path_style": True,
        },
    }
    assert "test-secret-key" not in str(storage.list_repositories())
    assert storage.get_repository_provider_credentials(repo_id) == {
        "access_key_id": "test-access-key", "secret_access_key": "test-secret-key",
    }

    payload = _build_backup_command_payload({
        "repository_id": repo_id, "backend": "restic", "source_path": "/source", "destination_path": "ignored",
    }, "daily", "run-1", storage=storage)
    assert payload["destination_path"] == "s3:https://minio.example.test:9000/backer-test/agents/host-one"
    assert payload["backend_options"]["s3"]["secret_access_key"] == "test-secret-key"
    assert payload["backend_options"]["repository_password"] == "repo-password"


@pytest.mark.parametrize("field,value", [("bucket", ""), ("endpoint", "minio.example.test"), ("prefix", "../escape")])
def test_s3_config_rejects_incomplete_or_unsafe_values(field: str, value: object) -> None:
    with pytest.raises(S3ConfigError):
        parse_s3_config(config(**{field: value}))


def test_s3_minio_end_to_end(tmp_path: Path) -> None:
    endpoint = os.getenv("BACKER_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("BACKER_TEST_S3_ENDPOINT is not configured")
    manager = ToolManager()
    rclone = shutil.which("rclone") or manager.get_tool_path("rclone")
    restic = shutil.which("restic") or manager.get_tool_path("restic")
    if not rclone or not restic:
        pytest.skip("rclone and restic are required for the MinIO contract")

    bucket = "backer-protocol"
    access_key = os.environ["BACKER_TEST_S3_ACCESS_KEY"]
    secret_key = os.environ["BACKER_TEST_S3_SECRET_KEY"]
    remote = (
        f":s3,provider=Minio,access_key_id={access_key},secret_access_key={secret_key},"
        f"endpoint={endpoint}:{bucket}"
    )
    subprocess.run([str(rclone), "mkdir", remote], check=True, capture_output=True, text=True)

    backend = ResticBackend({
        "repository_password": "repository-password",
        "s3": config(
            bucket=bucket,
            prefix="agent/job",
            endpoint=endpoint,
            access_key_id=access_key,
            secret_access_key=secret_key,
        ),
    })
    repository = BackupDestination(f"s3:{endpoint}/{bucket}/agent/job")
    source, restored = tmp_path / "source", tmp_path / "restored"
    source.mkdir()
    (source / "keep.txt").write_text("v1")
    (source / "deleted.txt").write_text("remove")

    assert backend.test_connection(repository)[0]
    assert backend.backup(BackupSource(source), repository).success
    (source / "keep.txt").write_text("v2")
    (source / "deleted.txt").unlink()
    assert backend.backup(BackupSource(source), repository).success
    assert len(backend.list_snapshots(repository)) == 2
    assert backend.restore(repository, restored, snapshot="latest").success
    assert next(restored.rglob("keep.txt")).read_text() == "v2"
    assert not list(restored.rglob("deleted.txt"))
    assert backend.prune(repository, keep_last=1).success
    assert backend.check(repository).success
