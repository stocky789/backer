"""Authentication and token management for Backer server.

Provides JWT token generation and validation for agent authentication.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

logger = logging.getLogger(__name__)

# Token expiration: 24 hours by default
DEFAULT_TOKEN_EXPIRY_HOURS = 24
SECRET_KEY_ENV = "BACKER_JWT_SECRET"


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
        # Generate a temporary secret from random bytes
        import secrets
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


def get_client_id_from_token(token: str) -> str | None:
    """Extract client ID from a JWT token.

    Args:
        token: JWT token string

    Returns:
        Client ID if valid, None otherwise
    """
    claims = verify_agent_token(token)
    if claims:
        return claims.get("sub")
    return None
