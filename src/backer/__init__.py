"""Backer - Unified backup orchestration."""

try:
    from importlib.metadata import version
    __version__ = version("backer")
except Exception:
    __version__ = "0.0.0"  # Fallback for development
