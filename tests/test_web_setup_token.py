from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backer.server.app import create_app
from backer.server.web.auth import get_setup_token


def setup_data(token: str | None, public_url: str = "https://backer.example.test") -> dict[str, str]:
    data = {
        "username": "owner",
        "display_name": "Test Owner",
        "password": "test-admin-password",
        "confirm_password": "test-admin-password",
        "timezone": "Australia/Sydney",
        "public_url": public_url,
    }
    if token is not None:
        data["setup_token"] = token
    return data


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_setup_rejects_missing_or_wrong_bootstrap_token(tmp_path: Path, token: str | None) -> None:
    app = create_app(tmp_path)

    with TestClient(app) as client:
        response = client.post("/setup", data=setup_data(token))

    assert response.status_code == 200
    assert "Invalid setup token." in response.text
    assert app.state.storage.count_users() == 0


def test_setup_accepts_bootstrap_token_and_closes_after_setup(tmp_path: Path) -> None:
    app = create_app(tmp_path)

    with TestClient(app, raise_server_exceptions=False) as client:
        setup = client.get("/setup")
        assert get_setup_token() not in setup.text
        response = client.post("/setup", data=setup_data(get_setup_token()), follow_redirects=False)
        assert response.status_code == 303
        assert app.state.storage.count_users() == 1
        second_setup = client.post("/setup", data=setup_data(get_setup_token()), follow_redirects=False)
        assert second_setup.headers["location"] == "/"


def test_agents_page_links_windows_download_to_installer(tmp_path: Path) -> None:
    app = create_app(tmp_path)

    with TestClient(app) as client:
        client.post("/setup", data=setup_data(get_setup_token()))
        response = client.get("/agents")

    assert (
        'href="https://git.stockhome.com.au/stocky789/backer/releases/download/'
        'release-main/backer-agent-setup.exe"'
    ) in response.text
    assert "Backer desktop client" in response.text


def test_agents_page_uses_configured_public_url(tmp_path: Path) -> None:
    app = create_app(tmp_path)

    with TestClient(app) as client:
        client.post(
            "/setup",
            data=setup_data(get_setup_token(), "https://backer.example.test/proxy/"),
        )
        response = client.get("/agents")

    assert 'const serverUrl = "https://backer.example.test/proxy";' in response.text
    assert "window.location.origin" not in response.text
