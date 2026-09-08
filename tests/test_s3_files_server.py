from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backer.backends.s3_files import S3FilesBackend
from backer.server.app import _build_backup_command_payload, create_app
from backer.server.models import Client
from backer.server.web.auth import get_setup_token


@pytest.mark.parametrize("initialize", [True, False])
def test_s3_files_creation_and_agent_restore(tmp_path, monkeypatch, initialize):
    app = create_app(tmp_path)
    initializations = []

    def init(backend, destination):
        initializations.append((backend.config, destination.path))
        return SimpleNamespace(success=initialize, errors=[] if initialize else ["marker mismatch"])

    monkeypatch.setattr(S3FilesBackend, "init_repo", init)
    monkeypatch.setattr(S3FilesBackend, "test_connection", lambda *_: (True, "marker validated"))
    s3 = {
        "bucket": "backer-test", "prefix": "offsite", "endpoint": "https://s3.example.test",
        "region": "us-east-1", "access_key_id": "test-access", "secret_access_key": "test-secret",
    }
    with TestClient(app) as client:
        client.post("/setup", data={
            "username": "owner", "display_name": "Owner", "password": "test-admin-password",
            "confirm_password": "test-admin-password", "setup_token": get_setup_token(),
            "timezone": "Australia/Sydney", "public_url": "https://backer.example.test",
        })
        response = client.post("/api/v1/repositories", json={
            "name": "Unencrypted S3", "type": "s3", "format": "files", "s3": s3,
        })
        storage = app.state.storage
        if not initialize:
            assert response.status_code == 400
            assert "marker mismatch" in response.json()["detail"]
            assert not storage.list_repositories()
            return
        assert response.status_code == 200, response.text
        repo_id = response.json()["id"]
        assert initializations == [({"repository_id": repo_id, "s3": s3}, "s3://backer-test/offsite")]
        assert storage.get_repository_password(repo_id) is None
        assert "test-secret" not in str(storage.list_repositories())
        assert storage.get_repository_provider_credentials(repo_id)["secret_access_key"] == "test-secret"
        agent = Client(id="agent", name="Agent", hostname="agent", capabilities=["files-repository-v1"])
        storage.add_client(agent, "hash")
        job = {"repository_id": repo_id, "source_path": "/source", "client_id": "agent"}
        with pytest.raises(ValueError, match="Agent does not support"):
            _build_backup_command_payload(job, "daily", "run", storage=storage, client_id="agent")
        storage.save_job("daily", job)
        rejected = client.post("/api/v1/restore", json={"job_name": "daily", "client_id": "agent"})
        assert rejected.status_code == 400
        assert "Agent does not support" in rejected.json()["detail"]
        android = Client(
            id="android", name="Android", hostname="phone", os_info="Android 16",
            capabilities=["files-s3-repository-v1"],
        )
        storage.add_client(android, "hash")
        rejected = client.post("/api/v1/restore", json={"job_name": "daily", "client_id": "android"})
        assert rejected.status_code == 400
        assert "Android agents support local repositories only" in rejected.json()["detail"]
        assert storage.get_pending_commands("agent") == []
        assert storage.get_pending_commands("android") == []
        assert storage.get_job_runs("restore:daily") == []
        agent.capabilities.append("files-s3-repository-v1")
        storage.update_client_capabilities(agent.id, agent.capabilities)
        payload = _build_backup_command_payload(job, "daily", "run", storage=storage, client_id="agent")
        assert payload["destination_path"] == "s3://backer-test/offsite/Agents/daily"
        assert payload["repository_options"] == {
            "format": "files", "repository_id": repo_id, "job_name": "daily", "s3": s3,
        }
        storage.save_job("daily", job)
        restored = client.post("/api/v1/restore", json={"job_name": "daily", "client_id": "agent"})
        assert restored.status_code == 200, restored.text
        command = storage.get_pending_commands("agent")[-1]
        assert command["command_type"] == "restore"
        assert command["payload"]["source_path"] == payload["destination_path"]
        assert command["payload"]["repository_options"] == payload["repository_options"]
