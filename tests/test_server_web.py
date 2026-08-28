from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backer.server.app import create_app
from backer.server.auth import (
    generate_agent_token,
    get_jwt_secret,
    hash_enrollment_code,
    verify_agent_token,
)
from backer.server.web.auth import get_setup_token, hash_password, verify_password


def complete_setup(client: TestClient, username: str = "owner", password: str = "test-admin-password") -> None:
    response = client.post(
        "/setup",
        data={
            "username": username,
            "display_name": "Test Owner",
            "password": password,
            "confirm_password": password,
            "setup_token": get_setup_token(),
            "timezone": "Australia/Sydney",
            "public_url": "https://backer.example.test",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


@pytest.fixture
def authenticated_client(tmp_path: Path):
    app = create_app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        complete_setup(client)
        yield client


def test_new_job_page_has_no_backup_engine_choice(authenticated_client) -> None:
    html = authenticated_client.get("/jobs/new").text
    assert "Backup Method" not in html
    assert 'name="backend"' not in html
    assert "obsolete" not in html


def test_jobs_page_has_no_backend_editor(authenticated_client) -> None:
    html = authenticated_client.get("/jobs").text
    assert 'id="editBackend"' not in html
    assert 'id="editObsoletePassword"' not in html


def test_repository_page_uses_product_language(authenticated_client) -> None:
    html = authenticated_client.get("/storage").text
    assert "S3-compatible storage" in html
    assert "Encryption password" in html
    assert "obsolete" not in html
    assert "Proxy / Kopia" not in html
    assert "path-style" not in html.lower()


def test_repository_connection_step_always_requests_encryption_password(authenticated_client) -> None:
    html = authenticated_client.get("/storage").text
    connection_step = html.split('<div id="step1">', 1)[1].split('<!-- Step 2:', 1)[0]
    assert 'id="repositoryPassword"' in connection_step


def test_new_server_requires_setup_before_login(tmp_path: Path) -> None:
    app = create_app(tmp_path)

    with TestClient(app, raise_server_exceptions=False) as client:
        client.cookies.set("backer_session", "stale-session-token")
        response = client.get("/login", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/setup"

        setup = client.get("/setup")
        assert setup.status_code == 200
        assert "Set up Backer" in setup.text

        assert client.get("/api/v1/clients").status_code == 503


def test_setup_lists_worldwide_timezones(tmp_path: Path) -> None:
    app = create_app(tmp_path)

    with TestClient(app) as client:
        setup = client.get("/setup")

    assert 'value="Pacific/Chatham"' in setup.text
    assert 'value="Africa/Cairo"' in setup.text


def test_setup_creates_account_and_initial_settings(tmp_path: Path) -> None:
    app = create_app(tmp_path)

    with TestClient(app, raise_server_exceptions=False) as client:
        complete_setup(client)
        assert app.state.storage.count_users() == 1
        assert app.state.storage.get_setting("timezone") == "Australia/Sydney"
        assert app.state.storage.get_setting("public_url") == "https://backer.example.test"
        assert client.get("/api/v1/clients").status_code == 200



def test_management_api_requires_login_but_browser_session_works(tmp_path: Path) -> None:
    app = create_app(tmp_path)

    with TestClient(app, raise_server_exceptions=False) as client:
        complete_setup(client, username="admin")
        client.cookies.clear()
        response = client.get("/api/v1/clients")
        assert response.status_code == 401
        assert response.json() == {"detail": "Authentication required"}
        assert client.get("/api/v1/clients", auth=("admin", "test-admin-password")).status_code == 200

        login = client.post(
            "/login",
            data={"username": "admin", "password": "test-admin-password"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        assert client.get("/api/v1/clients").status_code == 200

        token_response = client.post("/agents/enrollment-token")
        assert token_response.status_code == 200
        assert token_response.json()["token"]


def test_new_server_does_not_create_an_account_before_setup(tmp_path: Path) -> None:
    app = create_app(tmp_path)

    admin = app.state.storage.get_user_by_username("admin")
    assert admin is None


def test_registration_requires_single_use_token_and_proves_existing_identity(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    token = "K4M2-9XTP"
    app.state.storage.set_setting("agent_enrollment_token_hash", hash_enrollment_code(token))
    body = {"hostname": "agent-one", "version": "0.7.1", "enrollment_token": token}

    with TestClient(app, raise_server_exceptions=False) as client:
        complete_setup(client)
        first = client.post("/api/v1/clients/register", json=body)
        assert first.status_code == 200
        client_id = first.json()["client_id"]
        client_secret = first.json()["client_secret"]

        assert client.post("/api/v1/clients/register", json=body).status_code == 401
        assert client.post(
            "/api/v1/clients/register", json=body, auth=(client_id, client_secret)
        ).status_code == 200

        # Agent endpoints reach their own Basic-auth check instead of being
        # redirected by browser-session middleware.
        assert client.post("/api/v1/results", json={}).status_code == 401


def test_expired_enrollment_key_is_rejected(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    token = "K4M2-9XTP"
    app.state.storage.set_setting("agent_enrollment_token_hash", hash_enrollment_code(token))
    app.state.storage.set_setting("agent_enrollment_token_expires", "2020-01-01T00:00:00+00:00")

    with TestClient(app, raise_server_exceptions=False) as client:
        complete_setup(client)
        response = client.post(
            "/api/v1/clients/register",
            json={"hostname": "agent-late", "version": "0.7.1", "enrollment_token": token.lower()},
        )
        assert response.status_code == 403


def test_enrollment_token_consumption_is_atomic(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    token_hash = hashlib.sha256(b"single-use-token").hexdigest()
    app.state.storage.set_setting("agent_enrollment_token_hash", token_hash)

    assert app.state.storage.consume_setting("agent_enrollment_token_hash", token_hash)
    assert not app.state.storage.consume_setting("agent_enrollment_token_hash", token_hash)


def test_jwt_fallback_is_stable_for_the_server_process(monkeypatch) -> None:
    monkeypatch.delenv("BACKER_JWT_SECRET", raising=False)
    get_jwt_secret.cache_clear()

    token = generate_agent_token("agent-one")
    assert verify_agent_token(token) is not None

    get_jwt_secret.cache_clear()


def test_new_password_hashes_are_slow_and_legacy_hashes_still_verify() -> None:
    password_hash = hash_password("correct horse battery staple")
    assert password_hash.startswith("pbkdf2_sha256$")
    assert verify_password("correct horse battery staple", password_hash)

    legacy_salt = "legacy-salt"
    legacy_hash = hashlib.sha256((legacy_salt + "old-password").encode()).hexdigest()
    assert verify_password("old-password", f"{legacy_salt}:{legacy_hash}")


def test_navigation_is_a_single_sidebar_with_live_links(tmp_path: Path) -> None:
    """One nav surface, no dupes, and every link actually resolves."""
    import re

    app = create_app(tmp_path)

    with TestClient(app) as client:
        complete_setup(client)
        page = client.get("/").text

        hrefs = re.findall(r'<a href="([^"]+)" class="nav-item', page)
        assert hrefs == sorted(set(hrefs), key=hrefs.index), "duplicate sidebar links"
        assert set(hrefs) == {
            "/", "/jobs", "/restore", "/history", "/agents", "/hypervisors", "/storage", "/logs", "/settings",
        }

        for href in hrefs:
            assert client.get(href).status_code == 200, href
