"""Authentication and token management for Backer server.

Provides JWT token generation and validation for agent authentication.
"""

import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import jwt

logger = logging.getLogger(__name__)

# Token expiration: 24 hours by default
DEFAULT_TOKEN_EXPIRY_HOURS = 24
PROXY_CAPABILITY_EXPIRY_SECONDS = 15 * 60
SECRET_KEY_ENV = "BACKER_JWT_SECRET"


@lru_cache(maxsize=1)
def get_jwt_secret() -> str:
    """Get JWT secret key from environment or generate a default.

    In production, this should be a secure, persistent secret stored in
    environment variables or a secure config file.
    """
    secret = os.getenv(SECRET_KEY_ENV)
    if not secret:
        logger.warning(
            f"[AUTH] No {SECRET_KEY_ENV} environment variable set. "
            "Using a temporary secret - tokens will be invalid after server restart. "
            "Set BACKER_JWT_SECRET to a secure, persistent value in production."
        )
        # Keep a temporary secret for this server process. Generating one per
        # call made freshly issued tokens unverifiable.
        secret = secrets.token_urlsafe(32)
    return secret


def generate_agent_token(
    client_id: str,
    expires_in_hours: int = DEFAULT_TOKEN_EXPIRY_HOURS,
    additional_claims: dict[str, Any] | None = None,
) -> str:
    """Generate a JWT token for an agent/client.

    Args:
        client_id: The client/agent ID
        expires_in_hours: Token expiration time in hours
        additional_claims: Optional additional JWT claims

    Returns:
        JWT token string
    """
    secret = get_jwt_secret()
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(hours=expires_in_hours)

    claims = {
        "sub": client_id,  # Subject (client ID)
        "iat": now,  # Issued at
        "exp": expiry,  # Expiration
        "type": "agent",  # Token type
    }

    if additional_claims:
        claims.update(additional_claims)

    token = jwt.encode(claims, secret, algorithm="HS256")
    logger.debug(f"[AUTH] Generated token for client {client_id}, expires at {expiry.isoformat()}")
    return token


def verify_agent_token(token: str) -> dict[str, Any] | None:
    """Verify and decode a JWT token.

    Args:
        token: JWT token string

    Returns:
        Token claims if valid, None if invalid
    """
    secret = get_jwt_secret()
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"])

        # Verify token type
        if claims.get("type") != "agent":
            logger.warning(f"[AUTH] Token has unexpected type: {claims.get('type')}")
            return None

        logger.debug(f"[AUTH] Token valid for client: {claims.get('sub')}")
        return claims
    except jwt.ExpiredSignatureError:
        logger.debug("[AUTH] Token has expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning(f"[AUTH] Invalid token: {e}")
        return None
    except Exception as e:
        logger.error(f"[AUTH] Error verifying token: {e}")
        return None


def generate_proxy_capability(
    *, client_id: str, repo_id: str, job_name: str, run_id: str, subfolder: str, operation: str
) -> str:
    """Issue the narrowly-scoped credential used by proxy data endpoints."""
    now = datetime.now(timezone.utc)
    return jwt.encode({
        "sub": client_id, "repo": repo_id, "job": job_name, "run": run_id,
        "subfolder": subfolder, "operation": operation, "iat": now,
        "exp": now + timedelta(seconds=PROXY_CAPABILITY_EXPIRY_SECONDS), "type": "proxy-capability",
    }, get_jwt_secret(), algorithm="HS256")


def verify_proxy_capability(token: str) -> dict[str, Any] | None:
    """Return a valid proxy capability's claims, or ``None``."""
    try:
        claims = jwt.decode(token, get_jwt_secret(), algorithms=["HS256"])
        return claims if claims.get("type") == "proxy-capability" else None
    except jwt.InvalidTokenError as e:
        logger.warning(f"[AUTH] Invalid token: {e}")
        return None
    except Exception as e:
        logger.error(f"[AUTH] Error verifying token: {e}")
        return None


def verify_expired_proxy_capability(token: str) -> dict[str, Any] | None:
    """Return signed, expired proxy claims; callers must re-authorize them."""
    try:
        claims = jwt.decode(
            token, get_jwt_secret(), algorithms=["HS256"], options={"verify_exp": False},
        )
        expires_at = claims.get("exp", float("inf"))
        if claims.get("type") != "proxy-capability" or expires_at >= datetime.now(timezone.utc).timestamp():
            return None
        return claims
    except jwt.InvalidTokenError as e:
        logger.warning(f"[AUTH] Invalid token: {e}")
        return None
    except Exception as e:
        logger.error(f"[AUTH] Error verifying token: {e}")
        return None


# Enrollment keys are read off a screen and typed on another device (often a
# phone), so use an alphabet without look-alike characters and keep it short.
ENROLLMENT_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
ENROLLMENT_CODE_TTL = timedelta(minutes=15)


def generate_enrollment_code() -> str:
    """Generate a short, single-use enrollment key like 'K4M2-9XTP'."""
    code = "".join(secrets.choice(ENROLLMENT_ALPHABET) for _ in range(8))
    return f"{code[:4]}-{code[4:]}"


def hash_enrollment_code(code: str | None) -> str:
    """Hash an enrollment key, ignoring case and any separators typed by hand."""
    normalized = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    return hashlib.sha256(normalized.encode()).hexdigest()


def enrollment_code_expiry() -> str:
    """ISO timestamp for when a freshly issued enrollment key stops working."""
    return (datetime.now(timezone.utc) + ENROLLMENT_CODE_TTL).isoformat()


def enrollment_code_expired(expires_at: str | None) -> bool:
    """Check a stored enrollment expiry timestamp."""
    if not expires_at:
        return False
    try:
        return datetime.now(timezone.utc) > datetime.fromisoformat(expires_at)
    except ValueError:
        return True
