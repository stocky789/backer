"""Backer - Unified backup orchestration."""

# Import version from centralized _version.py (works in frozen exes too)
try:
    from backer._version import __version__ as __version__
except ImportError:
    __version__ = "0.0.0"  # Fallback for editable installs or test environments
