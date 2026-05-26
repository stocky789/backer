"""Web authentication for Backer server."""

import hashlib
import logging
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

# In-memory session store (for simplicity - could be moved to database)
_sessions: dict[str, dict[str, Any]] = {}


def hash_password(password: str) -> str:
    """Hash a password using SHA-256 with salt.

    For production, consider using bcrypt or argon2.
    """
    salt = secrets.token_hex(16)
    hash_value = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{hash_value}"


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its hash."""
    try:
        salt, hash_value = password_hash.split(":", 1)
        check_hash = hashlib.sha256((salt + password).encode()).hexdigest()
        return secrets.compare_digest(hash_value, check_hash)
    except (ValueError, AttributeError):
        return False


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


def ensure_default_user(storage: Storage) -> None:
    """Ensure at least one admin user exists. Creates default admin/admin if none exist."""
    if storage.count_users() == 0:
        logger.info("No users found, creating default admin user (admin/admin)")
        password_hash = hash_password("admin")
        storage.create_user(
            username="admin",
            password_hash=password_hash,
            display_name="Administrator",
            email=None,
            role="admin",
        )


def set_session_cookie(response: Response, token: str) -> None:
    """Set session cookie on response."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # Set to True if using HTTPS
    )


def clear_session_cookie(response: Response) -> None:
    """Clear session cookie."""
    response.delete_cookie(key=SESSION_COOKIE_NAME)


# Public paths that don't require authentication
PUBLIC_PATHS = {
    "/login",
    "/static",
    "/health",
    "/api/v1/clients/register",
    "/api/v1/clients/heartbeat",
    "/api/v1/commands",
    "/api/v1/results",
    "/api/v1/progress",
    "/api/v1/browse",
}


def is_public_path(path: str) -> bool:
    """Check if a path is public (doesn't require authentication)."""
    # Exact matches
    if path in PUBLIC_PATHS:
        return True

    # Prefix matches
    for public_path in PUBLIC_PATHS:
        if path.startswith(public_path + "/") or path.startswith(public_path + "?"):
            return True

    # API paths that use HTTP Basic auth (agent endpoints)
    if path.startswith("/api/v1/"):
        # These are authenticated via HTTP Basic, not session
        return True

    # Proxy repository API paths (authenticated via HTTP Basic or JWT)
    if path.startswith("/api/repo/"):
        return True

    return False


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware to enforce authentication on web routes."""

    async def dispatch(self, request: Request, call_next):
        # Check if path requires authentication
        path = request.url.path

        # Public paths don't need auth
        if is_public_path(path):
            return await call_next(request)

        # Check for valid session
        user = get_current_user(request)
        if user:
            # Attach user to request state for use in routes
            request.state.user = user
            return await call_next(request)

        # No valid session - redirect to login
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/login", status_code=303)
