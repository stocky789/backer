"""Backer server - central backup management daemon."""

__all__ = ["create_app", "BackerServer"]


def __getattr__(name):
    """Lazy import of server components to avoid circular imports."""
    if name == "create_app":
        from backer.server.app import create_app
        return create_app
    elif name == "BackerServer":
        from backer.server.daemon import BackerServer
        return BackerServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
