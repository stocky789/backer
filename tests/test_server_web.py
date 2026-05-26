from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from backer.server.app import create_app


def test_login_page_renders_with_stale_session_cookie(tmp_path: Path) -> None:
    app = create_app(tmp_path)

    with TestClient(app, raise_server_exceptions=False) as client:
        client.cookies.set("backer_session", "stale-session-token")
        response = client.get("/login")

    assert response.status_code == 200
    assert "Sign In" in response.text
