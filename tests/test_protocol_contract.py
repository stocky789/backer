"""Release gates for the advertised backup protocol."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backer.agent.service import AgentService
from backer.server.app import _build_backup_command_payload, create_app
from backer.server.auth import generate_proxy_capability
from backer.server.models import Client, ClientStatus, JobCreate
from backer.server.web.auth import get_setup_token


def _archive(files: dict[str, str]) -> bytes:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, content in files.items():
            payload = content.encode()
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    return data.getvalue()


def _complete_setup(client: TestClient) -> None:
    response = client.post(
        "/setup",
        data={
            "username": "owner",
            "display_name": "Owner",
            "password": "test-admin-password",
            "confirm_password": "test-admin-password",
            "setup_token": get_setup_token(),
            "timezone": "Australia/Sydney",
            "public_url": "http://backer.test",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_job_contract_has_no_backend_selector() -> None:
    job = JobCreate(name="photos", source_path="/photos", repository_id="repo-1")

    assert "backend" not in job.model_dump()
    assert "backend_options" not in job.model_dump()


@pytest.mark.parametrize("field", ["backend", "backend_options"])
def test_job_contract_rejects_removed_backend_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        JobCreate.model_validate({
            "name": "photos",
            "source_path": "/photos",
            "repository_id": "repo-1",
            field: "restic" if field == "backend" else {},
        })


def test_job_api_creates_an_enabled_engine_free_job(tmp_path: Path) -> None:
    app = create_app(tmp_path)

    with TestClient(app, raise_server_exceptions=False) as client:
        _complete_setup(client)
        response = client.post("/api/v1/jobs", json={"name": "photos", "source_path": "/photos"})

    assert response.status_code == 200
    assert response.json()["enabled"] is True


@pytest.mark.parametrize("field", ["backend", "backend_options"])
def test_job_update_rejects_removed_backend_fields(tmp_path: Path, field: str) -> None:
    app = create_app(tmp_path)
    app.state.storage.save_job("photos", {"source_path": "/photos"})

    with TestClient(app, raise_server_exceptions=False) as client:
        _complete_setup(client)
        response = client.put(
            "/api/v1/jobs/photos",
            json={field: "restic" if field == "backend" else {}},
        )

    assert response.status_code == 422


@pytest.mark.parametrize("field", ["backend", "backend_type", "backend_options"])
def test_repository_api_rejects_removed_backend_fields(tmp_path: Path, field: str) -> None:
    app = create_app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        _complete_setup(client)
        response = client.post("/api/v1/repositories", json={
            "name": "Backups",
            "type": "local",
            "share": str(tmp_path / "backups"),
            "repository_password": "secret",
            field: "restic" if field != "backend_options" else {},
        })

    assert response.status_code == 422


def test_backup_payload_keeps_smb_and_repository_passwords_separate() -> None:
    class Storage:
        def get_client(self, client_id: str) -> None:
            return None

        def get_repository(self, repo_id: str) -> dict[str, str]:
            assert repo_id == "repo-1"
            return {"repo_type": "smb", "server": "nas", "share": "backups", "username": "backup"}

        def get_storage_password(self, repo_id: str) -> str:
            assert repo_id == "repo-1"
            return "smb-only-secret"

        def get_repository_password(self, repo_id: str) -> str:
            assert repo_id == "repo-1"
            return "repository-only-secret"

    payload = _build_backup_command_payload(
        {
            "backend": "restic",
            "repository_id": "repo-1",
            "source_path": "/source",
            "destination_path": "//nas/backups",
            "backend_options": {"repository_password": "repository-only-secret"},
        },
        "photos",
        "run-1",
        storage=Storage(),
    )

    assert payload["smb_password"] == "smb-only-secret"
    assert payload["backend_options"]["repository_password"] == "repository-only-secret"
    assert "password" not in payload["backend_options"]


def test_agent_payload_redaction_covers_both_password_classes(tmp_path: Path) -> None:
    agent = AgentService("http://server", "agent-1", "agent-secret", tools_dir=tmp_path / "tools")

    safe = agent._redact_sensitive_data(
        {"smb_password": "smb-only-secret", "backend_options": {"repository_password": "repo-only-secret"}}
    )

    assert safe == {"smb_password": "***REDACTED***", "backend_options": {"repository_password": "***REDACTED***"}}


def test_proxy_capability_denies_a_token_scoped_to_another_job(tmp_path: Path) -> None:
    app = create_app(tmp_path / "server")
    storage_path = tmp_path / "repository"
    storage_path.mkdir()
    storage = app.state.storage
    storage.add_repository("repo-1", "local", "local", share=str(storage_path))
    storage.add_client(
        Client(
            id="agent-1",
            name="agent-1",
            hostname="agent-1",
            ip_address="127.0.0.1",
            status=ClientStatus.ONLINE,
            registered_at=datetime.now(),
            version="test",
        ),
        hashlib.sha256(b"agent-secret").hexdigest(),
    )
    wrong_job_token = generate_proxy_capability(
        client_id="agent-1", repo_id="repo-1", job_name="other-job", run_id="run-1",
        subfolder="Agents/other-job", operation="backup",
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        _complete_setup(client)
        response = client.post(
            "/api/repo/repo-1/backup",
            content=_archive({"keep.txt": "new"}),
            auth=("agent-1", "agent-secret"),
            headers={"X-Backup-Subfolder": "Agents/photos", "X-Backer-Capability": wrong_job_token},
        )

    assert response.status_code == 403


@pytest.mark.parametrize("operation", ["backup", "restore"])
def test_proxy_availability_check_accepts_queued_operation_capability(
    tmp_path: Path, operation: str
) -> None:
    app = create_app(tmp_path / "server")
    storage_path = tmp_path / "repository"
    storage_path.mkdir()
    storage = app.state.storage
    storage.add_repository("repo-1", "local", "local", share=str(storage_path))
    storage.add_client(
        Client(
            id="agent-1", name="agent-1", hostname="agent-1", ip_address="127.0.0.1",
            status=ClientStatus.ONLINE, registered_at=datetime.now(), version="test",
        ),
        hashlib.sha256(b"agent-secret").hexdigest(),
    )
    capability = generate_proxy_capability(
        client_id="agent-1", repo_id="repo-1", job_name="photos", run_id="run-1",
        subfolder="Agents/photos", operation=operation,
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        _complete_setup(client)
        response = client.get(
            "/api/repo/repo-1/check",
            auth=("agent-1", "agent-secret"),
            headers={"X-Backer-Capability": capability},
        )

    assert response.status_code == 200, response.text


def test_proxy_backup_replaces_deleted_files_before_snapshot(tmp_path: Path, monkeypatch) -> None:
    import backer.server.app as server_app

    snapshots: list[Path] = []

    class FakeKopia:
        def __init__(self, repo_path: str, password: str):
            self.repo_path = Path(repo_path)

        def ensure_repo(self) -> bool:
            return True

        def snapshot_create(self, source_dir: Path, **_: str) -> dict[str, str | bool]:
            snapshots.append(source_dir)
            return {"success": True, "snapshot_id": "snapshot-1"}

    monkeypatch.setattr(server_app, "ServerKopia", FakeKopia)
    app = create_app(tmp_path / "server")
    storage_path = tmp_path / "repository"
    storage_path.mkdir()
    storage = app.state.storage
    storage.add_repository("repo-1", "local", "local", share=str(storage_path))
    storage.add_client(
        Client(
            id="agent-1", name="agent-1", hostname="agent-1", ip_address="127.0.0.1",
            status=ClientStatus.ONLINE, registered_at=datetime.now(), version="test",
        ),
        hashlib.sha256(b"agent-secret").hexdigest(),
    )

    def backup(token: str, files: dict[str, str]) -> None:
        with TestClient(app, raise_server_exceptions=False) as client:
            if not app.state.storage.count_users():
                _complete_setup(client)
            response = client.post(
                "/api/repo/repo-1/backup",
                content=_archive(files),
                auth=("agent-1", "agent-secret"),
                headers={"X-Backup-Subfolder": "Agents/photos", "X-Backer-Capability": token},
            )
        assert response.status_code == 200, response.text
        assert response.json()["success"] is True

    backup(
        generate_proxy_capability(
            client_id="agent-1", repo_id="repo-1", job_name="photos", run_id="run-1",
            subfolder="Agents/photos", operation="backup",
        ),
        {"keep.txt": "old", "deleted.txt": "remove me"},
    )
    backup(
        generate_proxy_capability(
            client_id="agent-1", repo_id="repo-1", job_name="photos", run_id="run-2",
            subfolder="Agents/photos", operation="backup",
        ),
        {"keep.txt": "new"},
    )

    contents = storage_path / "Agents" / "photos" / "contents"
    assert (contents / "keep.txt").read_text() == "new"
    assert not (contents / "deleted.txt").exists()
    assert snapshots == [contents, contents]


def test_windows_startup_task_uses_the_dedicated_service_binary(monkeypatch, tmp_path: Path) -> None:
    from backer.client import windows_service

    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        commands.append(command)
        if command[1] == "/create":
            xml = Path(command[5]).read_text(encoding="utf-16")
            assert "backer-agent-service.exe" in xml
            assert "backer-agent.exe" not in xml
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(windows_service, "is_windows", lambda: True)
    monkeypatch.setattr(windows_service, "is_admin", lambda: True)
    monkeypatch.setattr(windows_service, "_prepare_service_config", lambda: None)
    monkeypatch.setattr(windows_service, "get_service_executable_path", lambda: r"C:\\Backer\\backer-agent-service.exe")
    monkeypatch.setattr(windows_service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(windows_service.subprocess, "run", fake_run)

    success, _ = windows_service.create_background_scheduled_task()

    assert success
    assert ["schtasks", "/run", "/tn", "BackerAgentService"] in commands


def _tool(name: str) -> str:
    from backer.tools.manager import ToolManager

    path = shutil.which(name) or ToolManager().get_tool_path(name)
    if not path:
        pytest.skip(f"{name} is not installed; local integration gate skipped")
    return str(path)


def _run(
    command: list[str], *, env: dict[str, str] | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
    if check:
        assert result.returncode == 0, f"{' '.join(command)}\n{result.stdout}\n{result.stderr}"
    return result


def test_rclone_local_mirror_contract(tmp_path: Path) -> None:
    rclone = _tool("rclone")
    source, repository, restored = tmp_path / "source", tmp_path / "repository", tmp_path / "restored"
    source.mkdir()
    (source / "keep.txt").write_text("v1")
    (source / "deleted.txt").write_text("remove")
    _run([rclone, "sync", str(source), str(repository)])

    (source / "keep.txt").write_text("v2")
    (source / "deleted.txt").unlink()
    _run([rclone, "sync", "--dry-run", str(source), str(repository)])
    assert (repository / "deleted.txt").exists()
    _run([rclone, "sync", str(source), str(repository)])
    assert not (repository / "deleted.txt").exists()
    assert _run([rclone, "lsf", str(repository)]).stdout.splitlines() == ["keep.txt"]
    _run([rclone, "sync", str(repository), str(restored)])
    assert (restored / "keep.txt").read_text() == "v2"


def test_restic_local_repository_contract(tmp_path: Path) -> None:
    restic = _tool("restic")
    source, repository, restored = tmp_path / "source", tmp_path / "repository", tmp_path / "restored"
    source.mkdir()
    (source / "keep.txt").write_text("v1")
    (source / "deleted.txt").write_text("remove")
    env = {**os.environ, "RESTIC_PASSWORD": "test-password"}
    _run([restic, "init", "--repo", str(repository)], env=env)
    _run([restic, "backup", "--repo", str(repository), str(source)], env=env)
    (source / "keep.txt").write_text("v2")
    (source / "deleted.txt").unlink()
    _run([restic, "backup", "--repo", str(repository), str(source)], env=env)
    snapshots = json.loads(_run([restic, "snapshots", "--json", "--repo", str(repository)], env=env).stdout)
    assert len(snapshots) == 2
    _run([restic, "restore", "latest", "--repo", str(repository), "--target", str(restored)], env=env)
    assert next(restored.rglob("keep.txt")).read_text() == "v2"
    assert not list(restored.rglob("deleted.txt"))
    sentinel = restored / "sentinel.txt"
    sentinel.write_text("do not delete")
    _run(
        [restic, "restore", "not-a-snapshot", "--repo", str(repository), "--target", str(restored)],
        env=env,
        check=False,
    )
    assert sentinel.read_text() == "do not delete"
    _run([restic, "check", "--repo", str(repository)], env=env)
    _run([restic, "forget", "--keep-last", "1", "--prune", "--repo", str(repository)], env=env)


def test_kopia_local_repository_contract(tmp_path: Path) -> None:
    kopia = _tool("kopia")
    source, repository = tmp_path / "source", tmp_path / "repository"
    source.mkdir()
    (source / "keep.txt").write_text("v1")
    (source / "deleted.txt").write_text("remove")
    config = tmp_path / "kopia.config"
    env = {**os.environ, "KOPIA_PASSWORD": "test-password", "KOPIA_CONFIG_PATH": str(config)}
    _run([kopia, "repository", "create", "filesystem", "--path", str(repository)], env=env)
    _run([kopia, "snapshot", "create", str(source)], env=env)
    (source / "keep.txt").write_text("v2")
    (source / "deleted.txt").unlink()
    _run([kopia, "snapshot", "create", str(source)], env=env)
    snapshots = json.loads(_run([kopia, "snapshot", "list", "--json"], env=env).stdout)
    assert len(snapshots) >= 2
    _run([kopia, "repository", "status"], env=env)
