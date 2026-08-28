from __future__ import annotations

from types import SimpleNamespace

from backer.server.app import _build_backup_command_payload, create_app


def _endpoint(app, path: str):
    return next(route.endpoint for route in app.routes if route.path == path)


def test_smb_transport_uses_storage_password_while_kopia_uses_repository_password(
    tmp_path, monkeypatch
) -> None:
    import backer.hypervisors.hyperv as hyperv_module
    import backer.server.app as app_module
    import backer.server.repositories as repositories

    app = create_app(tmp_path)
    storage = app.state.storage
    storage.add_repository(
        "repo", "share", "smb", server="files", username="backup",
        storage_password_encrypted=None, repository_password_encrypted=None,
    )
    storage.set_storage_password("repo", "storage-secret")
    storage.set_repository_password("repo", "repository-secret")

    captured: list[str | None] = []

    class InlineTasks:
        def submit(self, *, func, **_kwargs):
            func(SimpleNamespace(message="", progress=0))
            return SimpleNamespace(id="task")

    monkeypatch.setattr(app_module, "get_task_manager", lambda: InlineTasks())
    monkeypatch.setattr(
        repositories.SMBBrowser,
        "test_connection",
        staticmethod(lambda **kwargs: (captured.append(kwargs["password"]), "ok")),
    )
    _endpoint(app, "/api/v1/repositories/{repo_id}/test")("repo", storage)

    monkeypatch.setattr(
        repositories, "smb_list_files",
        lambda _server, _share, _path, _username, password, _domain: (captured.append(password), []),
    )
    _endpoint(app, "/api/v1/repositories/{repo_id}/import")("repo", storage)

    class FakeAPI:
        def __init__(self, **_kwargs):
            pass

    class FakeManager:
        def __init__(self, _api):
            pass

        def list_backups(self, **kwargs):
            captured.append(kwargs["smb_password"])
            return []

    monkeypatch.setattr(hyperv_module, "HyperVAPI", FakeAPI)
    monkeypatch.setattr(hyperv_module, "HyperVBackupManager", FakeManager)
    storage.add_hypervisor("hyperv", "Hyper-V", "hyperv", "hyperv.example", username="Administrator")
    storage.add_hypervisor_job("job", "job", "hyperv", [], "repo")
    _endpoint(app, "/api/v1/hypervisors/{hypervisor_id}/backups")("hyperv", storage=storage)

    payload = _build_backup_command_payload({"repository_id": "repo"}, "job", "run", storage=storage)
    assert captured == ["storage-secret", "storage-secret", "storage-secret"]
    assert payload["smb_password"] == "storage-secret"
    assert payload["repository_options"]["repository_password"] == "repository-secret"


def test_backup_payload_has_no_engine_selector() -> None:
    class Storage:
        def get_client(self, client_id: str) -> None:
            return None

        def get_repository(self, repo_id: str) -> dict[str, str]:
            assert repo_id == "repo"
            return {"repo_type": "smb", "server": "nas", "share": "backups"}

        def get_storage_password(self, repo_id: str) -> None:
            return None

        def get_repository_password(self, repo_id: str) -> str:
            return "secret"

    payload = _build_backup_command_payload(
        {"repository_id": "repo", "source_path": "/photos"}, "photos", "run-1", storage=Storage()
    )
    assert "backend" not in payload
    assert "backend_options" not in payload
    assert payload["repository_options"]["repository_password"] == "secret"
