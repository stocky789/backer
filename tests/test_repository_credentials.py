from pathlib import Path

from fastapi.testclient import TestClient

from backer.server.app import create_app
from backer.server.storage import Storage
from backer.server.web.auth import get_setup_token


def test_repository_credentials_are_separate_and_encrypted(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "backer.db")
    storage.add_repository(
        "repo", "share", "smb", storage_password_encrypted=None,
        repository_password_encrypted=None,
    )
    storage.set_storage_password("repo", "smb-secret")
    storage.set_repository_password("repo", "repo-secret")

    assert storage.get_storage_password("repo") == "smb-secret"
    assert storage.get_repository_password("repo") == "repo-secret"


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


def test_set_repository_password_endpoint_sets_password_on_legacy_repository(tmp_path: Path, caplog) -> None:
    app = create_app(tmp_path)
    storage = app.state.storage
    storage.add_repository(
        "repo", "share", "smb", storage_password_encrypted=None,
        repository_password_encrypted=None,
    )
    assert storage.get_repository_password("repo") is None

    with TestClient(app) as client:
        _setup(client)
        with caplog.at_level("INFO"):
            response = client.post(
                "/api/v1/repositories/repo/password",
                json={"repository_password": "new-repo-secret"},
            )

    assert response.status_code == 200
    assert response.json() == {"status": "updated", "repo_id": "repo"}
    assert "new-repo-secret" not in response.text
    assert storage.get_repository_password("repo") == "new-repo-secret"
    assert "new-repo-secret" not in caplog.text


def test_set_repository_password_endpoint_requires_auth(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    storage = app.state.storage
    storage.add_repository(
        "repo", "share", "smb", storage_password_encrypted=None,
        repository_password_encrypted=None,
    )

    with TestClient(app) as client:
        _setup(client)

    with TestClient(app) as anonymous:
        response = anonymous.post(
            "/api/v1/repositories/repo/password",
            json={"repository_password": "new-repo-secret"},
        )

    assert response.status_code == 401
    assert storage.get_repository_password("repo") is None


def test_set_repository_password_endpoint_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    storage = app.state.storage
    storage.add_repository(
        "repo", "share", "smb", storage_password_encrypted=None,
        repository_password_encrypted=None,
    )
    storage.set_repository_password("repo", "original-secret")

    with TestClient(app) as client:
        _setup(client)
        refused = client.post(
            "/api/v1/repositories/repo/password",
            json={"repository_password": "new-secret"},
        )
        assert refused.status_code == 409
        assert storage.get_repository_password("repo") == "original-secret"

        forced = client.post(
            "/api/v1/repositories/repo/password",
            json={"repository_password": "new-secret", "force": True},
        )

    assert forced.status_code == 200
    assert storage.get_repository_password("repo") == "new-secret"
