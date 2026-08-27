"""Web authentication for Backer server."""

import base64
import binascii
import hashlib
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from backer.server.storage import Storage

logger = logging.getLogger(__name__)

# Session configuration
SESSION_COOKIE_NAME = "backer_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 days in seconds
PASSWORD_HASH_ITERATIONS = 600_000
SETUP_TOKEN_ENV = "BACKER_SETUP_TOKEN"

# This credential must be available before the first HTTP request, so an
# operator can retrieve it from the server's startup logs.
_setup_token = os.environ.get(SETUP_TOKEN_ENV)
if not _setup_token:
    _setup_token = secrets.token_urlsafe(32)
    logger.warning("[AUTH] No %s set. First-run setup token: %s", SETUP_TOKEN_ENV, _setup_token)

# In-memory session store (for simplicity - could be moved to database)
_sessions: dict[str, dict[str, Any]] = {}


def get_setup_token() -> str:
    """Return the process-local first-run setup token."""
    return _setup_token


def verify_setup_token(token: str) -> bool:
    """Return whether a first-run setup token matches the process credential."""
    return secrets.compare_digest(token, _setup_token)


def hash_password(password: str) -> str:
    """Hash a password with the standard-library PBKDF2 implementation."""
    salt = secrets.token_bytes(16)
    hash_value = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_HASH_ITERATIONS)
    encoded_salt = base64.b64encode(salt).decode()
    encoded_hash = base64.b64encode(hash_value).decode()
    return f"pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}${encoded_salt}${encoded_hash}"


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its hash."""
    try:
        if password_hash.startswith("pbkdf2_sha256$"):
            _, iterations, salt, hash_value = password_hash.split("$", 3)
            check_hash = hashlib.pbkdf2_hmac(
                "sha256", password.encode(), base64.b64decode(salt), int(iterations)
            )
            return secrets.compare_digest(base64.b64encode(check_hash).decode(), hash_value)

        # Keep existing installations usable; successful logins are migrated
        # to PBKDF2 in the login handler.
        salt, hash_value = password_hash.split(":", 1)
        check_hash = hashlib.sha256((salt + password).encode()).hexdigest()
        return secrets.compare_digest(hash_value, check_hash)
    except (ValueError, AttributeError, TypeError, binascii.Error):
        return False


def needs_password_rehash(password_hash: str) -> bool:
    """Return whether a verified legacy hash should be upgraded."""
    return not password_hash.startswith("pbkdf2_sha256$")


def create_session(user_id: int, username: str, display_name: str) -> str:
    """Create a new session and return the session token."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    _sessions[token] = {
        "user_id": user_id,
        "username": username,
        "display_name": display_name,
        "created_at": now,
        "expires_at": now + timedelta(seconds=SESSION_MAX_AGE),
    }
    return token


def get_session(token: str) -> dict[str, Any] | None:
    """Get session data by token. Returns None if expired or invalid."""
    session = _sessions.get(token)
    if not session:
        return None

    if datetime.now(UTC) > session["expires_at"]:
        # Session expired, remove it
        del _sessions[token]
        return None

    return session


def destroy_session(token: str) -> None:
    """Destroy a session."""
    _sessions.pop(token, None)


def get_current_user(request: Request) -> dict[str, Any] | None:
    """Get current user from request session."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    return get_session(token)


def set_session_cookie(response: Response, token: str, secure: bool = False) -> None:
    """Set session cookie on response."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=secure,
    )


def clear_session_cookie(response: Response) -> None:
    """Clear session cookie."""
    response.delete_cookie(key=SESSION_COOKIE_NAME)


# Public paths that don't require authentication
PUBLIC_PATHS = {
    "/login",
    "/health",
}


def is_agent_path(path: str, method: str) -> bool:
    """Return whether an endpoint authenticates agents in its handler."""
    if method == "POST" and path in {
        "/api/v1/clients/register",
        "/api/v1/clients/token",
        "/api/v1/clients/heartbeat",
        "/api/v1/results",
        "/api/v1/progress",
    }:
        return True

    if method == "POST" and path.startswith("/api/v1/commands/") and path.endswith("/ack"):
        command_id = path.removeprefix("/api/v1/commands/").removesuffix("/ack")
        return command_id.isdigit()

    if method == "POST" and path.startswith("/api/v1/browse/") and path.endswith("/results"):
        request_id = path.removeprefix("/api/v1/browse/").removesuffix("/results")
        return bool(request_id) and "/" not in request_id

    return path.startswith("/api/repo/")


def is_public_path(path: str, method: str) -> bool:
    """Check whether middleware should defer authentication to the route."""
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return True

    return is_agent_path(path, method)


def get_basic_user(request: Request) -> dict[str, Any] | None:
    """Authenticate an admin HTTP Basic request for CLI API access."""
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "basic" or not value:
        return None

    try:
        username, password = base64.b64decode(value, validate=True).decode().split(":", 1)
    except (UnicodeDecodeError, ValueError):
        return None

    storage: Storage = request.app.state.storage
    user = storage.get_user_by_username(username)
    if not user or user.get("role") != "admin" or not verify_password(password, user.get("password_hash", "")):
        return None
    return user


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware to enforce authentication on web routes."""

    async def dispatch(self, request: Request, call_next):
        # Check if path requires authentication
        path = request.url.path

        # An empty installation has one public surface: creating its first
        # account. This also prevents agents being enrolled before an owner
        # has configured the server.
        if request.app.state.storage.count_users() == 0:
            if path == "/setup" or path == "/health" or path.startswith("/static/"):
                return await call_next(request)
            if path.startswith("/api/"):
                from fastapi.responses import JSONResponse
                return JSONResponse(status_code=503, content={"detail": "Complete setup first"})
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url="/setup", status_code=303)

        if path == "/setup":
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url="/", status_code=303)

        # Agent and proxy endpoints validate their own credentials.
        if is_public_path(path, request.method):
            return await call_next(request)

        # Check for valid session
        user = get_current_user(request)
        if user:
            # Attach user to request state for use in routes
            request.state.user = user
            return await call_next(request)

        # The browser uses sessions; HTTP Basic keeps the existing server CLI
        # usable without making management APIs public.
        user = get_basic_user(request)
        if user:
            request.state.user = user
            return await call_next(request)

        if path.startswith("/api/"):
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
                headers={"WWW-Authenticate": "Basic"},
            )

        # No valid session - redirect to login
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/login", status_code=303)
