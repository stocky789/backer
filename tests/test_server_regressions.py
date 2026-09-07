from __future__ import annotations

import hashlib
import io
import json
import tarfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from backer.server.app import _build_backup_command_payload, create_app
from backer.server.auth import generate_proxy_capability, hash_enrollment_code, verify_proxy_capability
from backer.server.models import Client, ClientStatus
from backer.server.web.auth import get_setup_token


def _client(storage, client_id="agent", hostname="agent", os_info="linux"):
    secret = "secret"
    storage.add_client(
        Client(
            id=client_id,
            name=hostname,
            hostname=hostname,
            os_info=os_info,
            status=ClientStatus.ONLINE,
            registered_at=datetime.now(),
            version="test",
        ),
        hashlib.sha256(secret.encode()).hexdigest(),
    )
    return secret


def _archive(files: dict[str, str]) -> bytes:
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as archive:
        for name, content in files.items():
            item = tarfile.TarInfo(name)
            encoded = content.encode()
            item.size = len(encoded)
            archive.addfile(item, io.BytesIO(encoded))
    return result.getvalue()


def _setup(client: TestClient) -> None:
    response = client.post(
        "/setup",
        data={
            "username": "owner",
            "display_name": "Owner",
            "password": "test-admin-password",
            "confirm_password": "test-admin-password",
            "setup_token": get_setup_token(),
            "timezone": "UTC",
            "public_url": "http://backer.test",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_duplicate_hostname_valid_token_creates_new_client(tmp_path: Path):
    app = create_app(tmp_path)
    storage = app.state.storage
    _client(storage, "first", "same-host")
    first_secret_hash = storage.get_client_secret_hash("first")
    token = "one-time"
    storage.set_setting("agent_enrollment_token_hash", hash_enrollment_code(token))

    with TestClient(app) as client:
        _setup(client)
        response = client.post(
            "/api/v1/clients/register", json={"hostname": "same-host", "version": "test", "enrollment_token": token}
        )

    assert response.status_code == 200
    assert response.json()["client_id"] != "first"
    assert storage.get_client("first").hostname == "same-host"
    assert storage.get_client_secret_hash("first") == first_secret_hash


def test_heartbeat_refreshes_proxy_capability(tmp_path: Path):
    app = create_app(tmp_path)
    storage = app.state.storage
    secret = _client(storage)
    storage.queue_command(
        "agent",
        "backup",
        {
            "job_name": "photos",
            "run_id": "run-1",
            "destination_path": "proxy://server/repo/repo-1/Agents/photos",
            "repository_options": {"proxy_capability": "stale"},
        },
    )

    with TestClient(app) as client:
        _setup(client)
        response = client.post("/api/v1/clients/heartbeat", json={"client_id": "agent"}, auth=("agent", secret))

    capability = response.json()["commands"][0]["payload"]["repository_options"]["proxy_capability"]
    assert verify_proxy_capability(capability)["subfolder"] == "Agents/photos"


def test_expired_proxy_capability_requires_its_pending_command(tmp_path: Path, monkeypatch):
    import backer.server.app as app_module
    import backer.server.auth as auth_module

    class FakeKopia:
        def __init__(self, *_):
            pass

        def ensure_repo(self, create_if_absent: bool = False):
            return True

        def snapshot_create(self, *_args, **_kwargs):
            return {"success": True, "snapshot_id": "snapshot-1"}

    monkeypatch.setattr(app_module, "ServerKopia", FakeKopia)
    monkeypatch.setattr(auth_module, "PROXY_CAPABILITY_EXPIRY_SECONDS", -1)
    app = create_app(tmp_path / "server")
    storage = app.state.storage
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    storage.add_repository("repo-1", "local", "local", share=str(repo_path))
    storage.set_repository_password("repo-1", "secret")
    secret = _client(storage)
    payload = {
        "job_name": "photos", "run_id": "run-1", "backend": "proxy",
        "destination_path": "proxy://server/repo/repo-1/Agents/photos",
    }
    command_id = storage.queue_command("agent", "backup", payload)
    capability = generate_proxy_capability(
        client_id="agent", repo_id="repo-1", job_name="photos", run_id="run-1",
        subfolder="Agents/photos", operation="backup",
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        _setup(client)
        def upload(token: str):
            return client.post(
                "/api/repo/repo-1/backup", content=_archive({"new.txt": "new"}), auth=("agent", secret),
                headers={"X-Backup-Subfolder": "Agents/photos", "X-Backer-Capability": token},
            )

        assert upload(capability).status_code == 200
        assert client.get(
            "/api/repo/repo-1/check", auth=("agent", secret),
            headers={"X-Backer-Capability": capability},
        ).status_code == 200
        assert upload(generate_proxy_capability(
            client_id="agent", repo_id="repo-1", job_name="other", run_id="run-1",
            subfolder="Agents/other", operation="backup",
        )).status_code == 403
        assert upload(f"{capability}x").status_code == 403
        assert client.post(f"/api/v1/commands/{command_id}/ack", auth=("agent", secret)).status_code == 200
        assert upload(capability).status_code == 403


def test_server_kopia_locks_latest_snapshot_once_per_repository(tmp_path: Path, monkeypatch) -> None:
    import backer.server.app as app_module

    locks: list[Path] = []

    @contextmanager
    def record_lock(path: Path):
        locks.append(path)
        yield

    monkeypatch.setattr(app_module, "file_lock", record_lock)
    monkeypatch.setattr(
        app_module.ServerKopia,
        "_snapshot_list",
        lambda *_: [{"full_id": "latest"}],
    )

    assert app_module.ServerKopia(str(tmp_path / "repo"), "password").find_latest_snapshot("photos") == "latest"
    assert locks == [(tmp_path / "repo").resolve() / ".backer-kopia.lock"]


def test_local_repository_check_works_without_posix_getuid(tmp_path: Path, monkeypatch) -> None:
    from backer.server.repositories import LocalBrowser

    monkeypatch.delattr("backer.server.repositories.os.getuid", raising=False)

    assert LocalBrowser.test_connection(str(tmp_path)) == (True, f"Local directory accessible: {tmp_path}")


def test_local_repository_scan_uses_its_encryption_password(tmp_path: Path, monkeypatch) -> None:
    import backer.server.app as app_module

    passwords: list[str] = []

    class FakeKopia:
        def __init__(self, _path, password):
            passwords.append(password)

        def snapshot_list(self, job_name=None):
            return []

    monkeypatch.setattr(app_module, "ServerKopia", FakeKopia)
    app = create_app(tmp_path / "server")
    storage = app.state.storage
    repository = tmp_path / "repository"
    (repository / ".kopia-repo").mkdir(parents=True)
    storage.add_repository("repo-1", "local", "local", share=str(repository))
    storage.set_repository_password("repo-1", "scan-password")

    with TestClient(app) as client:
        _setup(client)
        task_id = client.post("/api/v1/repositories/repo-1/scan").json()["task_id"]
        for _ in range(20):
            if client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "completed":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("repository scan did not complete")

    assert passwords == ["scan-password"]


def test_proxy_snapshot_failure_restores_previous_contents(tmp_path: Path, monkeypatch):
    import backer.server.app as app_module

    class FailingKopia:
        def __init__(self, *_):
            pass

        def ensure_repo(self, create_if_absent: bool = False):
            return True

        def snapshot_create(self, *_args, **_kwargs):
            return {"success": False, "error": "nope"}

    monkeypatch.setattr(app_module, "ServerKopia", FailingKopia)
    app = create_app(tmp_path / "server")
    repo_path = tmp_path / "repo"
    (repo_path / "Agents" / "photos" / "contents").mkdir(parents=True)
    (repo_path / "Agents" / "photos" / "contents" / "old.txt").write_text("old")
    storage = app.state.storage
    storage.add_repository("repo-1", "local", "local", share=str(repo_path))
    storage.set_repository_password("repo-1", "secret")
    storage.set_repository_password("repo-1", "secret")
    secret = _client(storage)
    capability = generate_proxy_capability(
        client_id="agent",
        repo_id="repo-1",
        job_name="photos",
        run_id="run-1",
        subfolder="Agents/photos",
        operation="backup",
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        _setup(client)
        response = client.post(
            "/api/repo/repo-1/backup",
            content=_archive({"new.txt": "new"}),
            auth=("agent", secret),
            headers={"X-Backup-Subfolder": "Agents/photos", "X-Backer-Capability": capability},
        )

    assert response.json()["success"] is False
    contents = repo_path / "Agents" / "photos" / "contents"
    assert (contents / "old.txt").read_text() == "old"
    assert not list(contents.parent.glob(".backer-*"))


def test_proxy_rollback_retains_previous_contents_when_restore_fails(tmp_path: Path, monkeypatch):
    import backer.server.app as app_module

    class FailingKopia:
        def __init__(self, *_):
            pass

        def ensure_repo(self, create_if_absent: bool = False):
            return True

        def snapshot_create(self, *_args, **_kwargs):
            return {"success": False, "error": "nope"}

    real_replace = app_module.os.replace

    def fail_restore(source, destination):
        if Path(source).name.startswith(".backer-previous-"):
            raise OSError("restore rename failed")
        return real_replace(source, destination)

    monkeypatch.setattr(app_module, "ServerKopia", FailingKopia)
    monkeypatch.setattr(app_module.os, "replace", fail_restore)
    app = create_app(tmp_path / "server")
    repo_path = tmp_path / "repo"
    contents = repo_path / "Agents" / "photos" / "contents"
    contents.mkdir(parents=True)
    (contents / "old.txt").write_text("old")
    storage = app.state.storage
    storage.add_repository("repo-1", "local", "local", share=str(repo_path))
    storage.set_repository_password("repo-1", "secret")
    secret = _client(storage)
    capability = generate_proxy_capability(
        client_id="agent",
        repo_id="repo-1",
        job_name="photos",
        run_id="run-1",
        subfolder="Agents/photos",
        operation="backup",
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        _setup(client)
        response = client.post(
            "/api/repo/repo-1/backup",
            content=_archive({"new.txt": "new"}),
            auth=("agent", secret),
            headers={"X-Backup-Subfolder": "Agents/photos", "X-Backer-Capability": capability},
        )

    assert response.json()["success"] is False
    assert response.json()["error"] == "nope"
    assert (contents / "old.txt").read_text() == "old"


def test_proxy_backup_succeeds_when_previous_cleanup_fails(tmp_path: Path, monkeypatch):
    import backer.server.app as app_module

    class SuccessfulKopia:
        def __init__(self, *_):
            pass

        def ensure_repo(self, create_if_absent: bool = False):
            return True

        def snapshot_create(self, *_args, **_kwargs):
            return {"success": True, "snapshot_id": "snapshot-1"}

    real_rmtree = app_module.shutil.rmtree

    def fail_previous_cleanup(path, *args, **kwargs):
        if Path(path).name.startswith(".backer-previous-"):
            raise OSError("cleanup failed")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(app_module, "ServerKopia", SuccessfulKopia)
    monkeypatch.setattr(app_module.shutil, "rmtree", fail_previous_cleanup)
    app = create_app(tmp_path / "server")
    repo_path = tmp_path / "repo"
    contents = repo_path / "Agents" / "photos" / "contents"
    contents.mkdir(parents=True)
    (contents / "old.txt").write_text("old")
    storage = app.state.storage
    storage.add_repository("repo-1", "local", "local", share=str(repo_path))
    storage.set_repository_password("repo-1", "secret")
    secret = _client(storage)
    capability = generate_proxy_capability(
        client_id="agent",
        repo_id="repo-1",
        job_name="photos",
        run_id="run-1",
        subfolder="Agents/photos",
        operation="backup",
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        _setup(client)
        response = client.post(
            "/api/repo/repo-1/backup",
            content=_archive({"new.txt": "new"}),
            auth=("agent", secret),
            headers={"X-Backup-Subfolder": "Agents/photos", "X-Backer-Capability": capability},
        )

    assert response.json()["success"] is True
    assert not contents.exists()  # encrypted proxy staging must not persist plaintext


def test_job_secret_sentinel_preserves_stored_value(tmp_path: Path):
    app = create_app(tmp_path)
    storage = app.state.storage
    storage.save_job(
        "photos",
        {
            "name": "photos",
            "source_path": "/src",
            "destination_path": "/dst",
                "repository_id": "repo",
        },
    )

    with TestClient(app) as client:
        _setup(client)
        shown = client.get("/api/v1/jobs/photos").json()
        response = client.put("/api/v1/jobs/photos", json=shown)

    assert response.status_code == 400


def test_nested_job_secret_sentinels_preserve_stored_values(tmp_path: Path):
    app = create_app(tmp_path)
    storage = app.state.storage
    storage.save_job(
        "photos",
        {
            "name": "photos",
            "source_path": "/src",
            "destination_path": "/dst",
                "repository_id": "repo",
        },
    )

    with TestClient(app) as client:
        _setup(client)
        shown = client.get("/api/v1/jobs/photos").json()
        assert client.put("/api/v1/jobs/photos", json=shown).status_code == 400



def test_android_s3_job_create_is_rejected_before_queueing(tmp_path: Path):
    app = create_app(tmp_path)
    storage = app.state.storage
    _client(storage, os_info="Android 15")
    storage.add_repository("s3-1", "s3", "s3", share="bucket")

    with TestClient(app) as client:
        _setup(client)
        response = client.post(
            "/api/v1/jobs",
            json={
                "name": "photos",
                "source_path": "/src",
                    "client_id": "agent",
                "repository_id": "s3-1",
            },
        )

    assert response.status_code == 400
    assert "Android" in response.json()["detail"]

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/jobs",
            json={
                "name": "no-repository",
                "source_path": "/src",
                "destination_path": "/dst",
                "client_id": "agent",
            },
            auth=("owner", "test-admin-password"),
        )
    assert response.status_code == 400

    _client(storage, "desktop", "desktop", "Linux")
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/jobs",
            json={
                "name": "desktop-photos",
                "source_path": "/src",
                "client_id": "desktop",
                "repository_id": "s3-1",
            },
            auth=("owner", "test-admin-password"),
        )
    assert response.status_code == 200
    assert storage.get_job("desktop-photos")["repository_id"] == "s3-1"


def test_android_remote_job_is_rejected_by_web_run_and_restore(tmp_path: Path):
    app = create_app(tmp_path)
    storage = app.state.storage
    _client(storage, "android", "android", "Android 15")
    storage.add_repository("smb-1", "smb", "smb", server="nas", share="backups")
    storage.save_job(
        "photos",
        {
            "name": "photos",
            "source_path": "/src",
            "destination_path": "//nas/backups",
            "repository_id": "smb-1",
        },
    )

    with TestClient(app) as client:
        _setup(client)
        web = client.post(
            "/jobs/create",
            data={
                "name": "web-photos",
                "source_path": "/src",
                "repository_id": "smb-1",
                "client_id": "android",
            },
            follow_redirects=False,
        )
        run = client.post("/api/v1/jobs/photos/run", json={"override_client_id": "android"})
        restore = client.post("/api/v1/restore", json={"job_name": "photos", "client_id": "android"})

    assert web.headers["location"] == "/jobs/new?error=android_local_repositories_only"
    assert run.status_code == restore.status_code == 400
    assert storage.get_job_runs("photos") == []
    assert storage.get_job_runs("restore:photos") == []
    assert storage.get_pending_commands("android") == []


def test_android_local_job_uses_proxy_payload(tmp_path: Path):
    app = create_app(tmp_path)
    storage = app.state.storage
    _client(storage, "android", "android", "Android 15")
    storage.add_repository("local-1", "local", "local", share=str(tmp_path / "repo"))
    storage.set_repository_password("local-1", "secret")

    payload = _build_backup_command_payload(
        {"repository_id": "local-1", "source_path": "/src"},
        "photos",
        "run-1",
        storage=storage,
        client_id="android",
    )

    assert payload["destination_path"].startswith("proxy://")
    assert payload["repository_options"]["proxy_capability"]


def test_android_restore_keeps_proxy_credentials_in_repository_options(tmp_path: Path):
    app = create_app(tmp_path)
    storage = app.state.storage
    _client(storage, "android", "android", "Android 15")
    storage.add_repository("local-1", "local", "local", share=str(tmp_path / "repo"))
    storage.set_repository_password("local-1", "secret")
    storage.save_job(
        "photos", {"name": "photos", "source_path": "/photos", "repository_id": "local-1"}
    )

    with TestClient(app) as client:
        _setup(client)
        response = client.post(
            "/api/v1/restore",
            json={"job_name": "photos", "client_id": "android", "source_subfolder": r"albums\\2026"},
        )

    assert response.status_code == 200
    payload = storage.get_pending_commands("android")[0]["payload"]
    assert payload["source_subfolder"] == "albums/2026"
    assert set(payload["repository_options"]) == {"format", "repository_password", "proxy_capability"}
    assert "proxy_capability" not in payload


def test_restore_rejects_unsafe_snapshot_subfolder_before_queueing(tmp_path: Path):
    app = create_app(tmp_path)
    storage = app.state.storage
    _client(storage)
    storage.add_repository("local-1", "local", "local", share=str(tmp_path / "repo"))
    storage.set_repository_password("local-1", "secret")
    storage.save_job("photos", {"name": "photos", "source_path": "/photos", "repository_id": "local-1"})

    with TestClient(app) as client:
        _setup(client)
        response = client.post(
            "/api/v1/restore",
            json={"job_name": "photos", "client_id": "agent", "source_subfolder": "../outside"},
        )

    assert response.status_code == 400
    assert storage.get_pending_commands("agent") == []
    assert storage.get_job_runs("restore:photos") == []


def test_proxy_restore_scopes_to_safe_subtree_and_own_job_snapshot(tmp_path: Path, monkeypatch):
    import backer.server.app as app_module

    restored: list[str] = []

    class FakeKopia:
        def __init__(self, *_):
            pass

        def ensure_repo(self, create_if_absent=False):
            return True

        def snapshot_list(self, job_name):
            assert job_name == "photos"
            return [{"id": "short-id", "full_id": "immutable-photo-snapshot"}]

        def snapshot_restore(self, snapshot_id, destination):
            restored.append(snapshot_id)
            nested = Path(destination) / "only" / "nested"
            nested.mkdir(parents=True)
            (nested / "restored.txt").write_text("snapshot")
            return {"success": True}

    monkeypatch.setattr(app_module, "ServerKopia", FakeKopia)
    app = create_app(tmp_path / "server")
    storage = app.state.storage
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    storage.add_repository("repo-1", "local", "local", share=str(repo_path))
    storage.set_repository_password("repo-1", "secret")
    secret = _client(storage)
    capability = generate_proxy_capability(
        client_id="agent", repo_id="repo-1", job_name="photos", run_id="restore-1",
        subfolder="Agents/photos", operation="restore",
    )
    headers = {"X-Restore-Subfolder": "Agents/photos", "X-Backer-Capability": capability}

    with TestClient(app, raise_server_exceptions=False) as client:
        _setup(client)
        unsafe = client.get(
            "/api/repo/repo-1/restore?include=../outside", auth=("agent", secret), headers=headers
        )
        foreign = client.get(
            "/api/repo/repo-1/restore?snapshot=other-job-snapshot", auth=("agent", secret), headers=headers
        )
        response = client.get(
            "/api/repo/repo-1/restore?snapshot=short-id&include=only%5Cnested",
            auth=("agent", secret), headers=headers,
        )

    assert unsafe.status_code == 400
    assert foreign.status_code == 404
    assert response.status_code == 200
    assert restored == ["immutable-photo-snapshot/only/nested"]
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
        assert archive.getnames() == ["only/nested/restored.txt"]


def test_imported_smb_restore_builds_repository_source_before_queueing(tmp_path: Path):
    app = create_app(tmp_path)
    storage = app.state.storage
    _client(storage)
    storage.add_repository("repo", "smb", "smb", server="nas", share="backups")
    storage.set_repository_password("repo", "secret")
    storage.save_job("photos", {"name": "photos", "source_path": "/photos", "repository_id": "repo"})

    with TestClient(app) as client:
        _setup(client)
        response = client.post("/api/v1/restore", json={"job_name": "photos", "client_id": "agent"})

    assert response.status_code == 200
    assert storage.get_pending_commands("agent")[0]["payload"]["source_path"] == "//nas/backups/Agents/photos"


def test_files_smb_creation_initializes_marker_before_saving(tmp_path: Path, monkeypatch):
    mounted_share = tmp_path / "mounted"
    mounted_share.mkdir()

    @contextmanager
    def mounted(*_args, **_kwargs):
        yield mounted_share

    monkeypatch.setattr("backer.core.mounts.smb_mount_context", mounted)
    app = create_app(tmp_path / "server")
    with TestClient(app) as client:
        _setup(client)
        response = client.post(
            "/api/v1/repositories",
            json={
                "name": "Plain SMB",
                "format": "files",
                "type": "smb",
                "server": "nas",
                "share": "backups",
                "path": "backer",
                "username": "user",
                "storage_password": "secret",
            },
        )

    assert response.status_code == 200, response.text
    marker = mounted_share / "backer" / ".backer" / "repository.json"
    assert marker.exists()
    assert json.loads(marker.read_text())["repository_id"] == response.json()["id"]


def test_files_proxy_backup_and_restore_use_immutable_snapshot(tmp_path: Path):
    from backer.backends.base import BackupDestination
    from backer.backends.files import FilesBackend

    app = create_app(tmp_path / "server")
    storage = app.state.storage
    repository = tmp_path / "repository"
    assert FilesBackend({"repository_id": "repo-1"}).init_repo(BackupDestination(str(repository))).success
    storage.add_repository("repo-1", "Plain", "local", share=str(repository), config={"format": "files"})
    secret = _client(storage)
    backup_capability = generate_proxy_capability(
        client_id="agent", repo_id="repo-1", job_name="photos", run_id="run-1",
        subfolder="Agents/photos", operation="backup",
    )
    restore_capability = generate_proxy_capability(
        client_id="agent", repo_id="repo-1", job_name="photos", run_id="restore-1",
        subfolder="Agents/photos", operation="restore",
    )

    with TestClient(app) as client:
        _setup(client)
        uploaded = client.post(
            "/api/repo/repo-1/backup",
            content=_archive({"photo.txt": "snapshot"}),
            auth=("agent", secret),
            headers={"X-Backup-Subfolder": "Agents/photos", "X-Backer-Capability": backup_capability},
        )
        restored = client.get(
            "/api/repo/repo-1/restore?snapshot=run-1",
            auth=("agent", secret),
            headers={"X-Restore-Subfolder": "Agents/photos", "X-Backer-Capability": restore_capability},
        )

    assert uploaded.status_code == 200 and uploaded.json()["snapshot_id"] == "run-1"
    assert restored.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(restored.content), mode="r:gz") as archive:
        assert archive.extractfile("photo.txt").read() == b"snapshot"
    assert (repository / "Agents" / "photos" / "snapshots" / "run-1" / "manifest.json").exists()


def test_files_smb_job_rejects_agent_without_files_capability(tmp_path: Path):
    app = create_app(tmp_path)
    storage = app.state.storage
    _client(storage)
    storage.add_repository(
        "repo-1", "Plain SMB", "smb", server="nas", share="backups", path="backer", config={"format": "files"}
    )

    with TestClient(app) as client:
        _setup(client)
        response = client.post(
            "/api/v1/jobs",
            json={"name": "photos", "source_path": "/photos", "repository_id": "repo-1", "client_id": "agent"},
        )

    assert response.status_code == 400
    assert "does not support files repositories" in response.text
    storage.update_client_capabilities("agent", ["files-repository-v1"])
    payload = _build_backup_command_payload(
        {"repository_id": "repo-1", "client_id": "agent", "source_path": "/photos"},
        "photos",
        "run-1",
        storage=storage,
    )
    assert payload["destination_path"] == "//nas/backups/backer/Agents/photos"
    assert payload["repository_options"]["repository_id"] == "repo-1"
    assert payload["repository_options"]["job_name"] == "photos"


def test_snapshot_capability_cannot_list_another_job(tmp_path: Path):
    app = create_app(tmp_path / "server")
    storage = app.state.storage
    repository = tmp_path / "repository"
    storage.add_repository(
        "repo-1", "Plain", "local", share=str(repository), config={"format": "files"}
    )
    secret = _client(storage)
    capability = generate_proxy_capability(
        client_id="agent",
        repo_id="repo-1",
        job_name="photos",
        run_id="run-1",
        subfolder="Agents/photos",
        operation="list",
    )

    with TestClient(app) as client:
        _setup(client)
        response = client.get(
            "/api/repo/repo-1/snapshots?job=documents",
            auth=("agent", secret),
            headers={"X-Backer-Capability": capability},
        )

    assert response.status_code == 403


def test_proxy_backup_without_storage_path_is_not_reported_as_success(tmp_path: Path):
    app = create_app(tmp_path / "server")
    storage = app.state.storage
    storage.add_repository("repo-1", "Broken", "local")
    storage.set_repository_password("repo-1", "secret")
    secret = _client(storage)
    capability = generate_proxy_capability(
        client_id="agent",
        repo_id="repo-1",
        job_name="photos",
        run_id="run-1",
        subfolder="Agents/photos",
        operation="backup",
    )

    with TestClient(app) as client:
        _setup(client)
        response = client.post(
            "/api/repo/repo-1/backup",
            content=_archive({"photo.jpg": "data"}),
            auth=("agent", secret),
            headers={"X-Backup-Subfolder": "Agents/photos", "X-Backer-Capability": capability},
        )

    assert response.status_code == 500
    assert response.json()["success"] is False
