"""Backer - Unified backup orchestration."""

# Try multiple sources for version:
# 1. _version.py (works in frozen exes and source installs)
# 2. importlib.metadata (works for pip-installed packages)
# 3. Fallback to "0.0.0"
try:
    from backer._version import __version__ as __version__
except ImportError:
    try:
        from importlib.metadata import version as _get_version
        __version__: str = _get_version("backer")
    except Exception:
        __version__ = "0.0.0"
