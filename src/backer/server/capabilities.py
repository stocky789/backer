"""The support contract enforced by the server."""

from dataclasses import dataclass


@dataclass(frozen=True)
class BackendCapabilities:
    repository_types: frozenset[str]
    snapshots: bool
    partial_restore: bool
    dry_run: bool
    init: bool
    prune: bool
    check: bool


BACKEND_CAPABILITIES = {
    "rclone": BackendCapabilities(frozenset({"local", "smb", "nfs"}), False, False, True, False, False, True),
    "restic": BackendCapabilities(frozenset({"local", "smb", "nfs", "s3"}), True, True, True, True, True, True),
    "kopia": BackendCapabilities(frozenset({"local", "smb", "nfs"}), True, True, False, True, True, True),
    # Local repositories are stored through the server's Kopia proxy, never an agent rsync backend.
    "proxy": BackendCapabilities(frozenset({"local"}), True, True, False, False, False, False),
}


def validate_job_backend(backend: str, repository_type: str | None = None) -> str | None:
    """Return a human-readable validation error, if the combination is unsupported."""
    capability = BACKEND_CAPABILITIES.get(backend)
    if not capability:
        return f"Unsupported backend: {backend}"
    if repository_type and repository_type not in capability.repository_types:
        return f"Backend '{backend}' does not support {repository_type} repositories"
    if repository_type == "local" and backend not in {"kopia", "proxy"}:
        return "Local repositories use the server proxy/Kopia backend"
    return None
