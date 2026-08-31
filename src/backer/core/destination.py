"""Prepare repository paths for backup and restore.

Tests that need a clean Windows SMB manager reset ``_smb_manager``.
"""

import sys
from typing import Any

from backer.core.mounts import (
    is_nfs_path,
    is_smb_path,
    nfs_mount_context,
    parse_nfs_path,
    parse_smb_path,
    smb_mount_context,
)

_smb_manager: Any | None = None


def prepare_destination(job: dict[str, Any], backend_name: str) -> tuple[str, Any]:
    """Prepare the destination path for the backend."""
    dest_path = job.get("destination_path", "")

    # Proxy backend uses destination_path as a proxy:// URI directly
    # Don't try to mount it as a filesystem path
    if backend_name == "proxy" or dest_path.startswith(("proxy://", "proxys://")):
        return dest_path, None

    # Windows can use UNC paths directly, but does not support the NFS
    # destination format used by the agent protocol.
    if sys.platform == "win32":
        if is_nfs_path(dest_path) or (job.get("nfs_server") and job.get("nfs_export")):
            raise RuntimeError("NFS destinations are not supported on Windows")
        _prepare_windows_smb(dest_path, job)
        return dest_path, None

    # Handle SMB paths
    if is_smb_path(dest_path):
        return _prepare_smb_destination(job, backend_name, dest_path)

    # Check if NFS credentials were passed (job linked to NFS repository)
    # This takes priority over parsing dest_path as NFS, because the server
    # provides the actual NFS export separately from the subpath
    nfs_server = job.get("nfs_server")
    nfs_export = job.get("nfs_export")
    if nfs_server and nfs_export:
        # Server provided NFS export - mount the export and calculate subpath
        # dest_path will be like: server:/export/path/Agents/jobname
        # nfs_export is the actual mountable export: /export/path
        # We need to extract the subpath after the export
        print(f"[NFS] Using NFS repository: {nfs_server}:{nfs_export}")

        # Calculate subpath: everything in dest_path after the export
        # dest_path format: "server:/export/subpath" or just "/local/path"
        subpath = ""
        if is_nfs_path(dest_path):
            # Parse the full destination to get the path portion
            _, full_path, _ = parse_nfs_path(dest_path)
            # Remove the export prefix to get subpath
            if full_path.startswith(nfs_export):
                subpath = full_path[len(nfs_export) :].lstrip("/")
            else:
                # Export doesn't match - maybe path format differs, use full path as subpath
                subpath = full_path.lstrip("/")

        ctx = nfs_mount_context(server=nfs_server, export_path=nfs_export)
        mount_path = ctx.__enter__()
        full_path = str(mount_path / subpath) if subpath else str(mount_path)
        print(f"[NFS] Using mounted path: {full_path}")
        return full_path, ctx

    # Handle NFS paths (without server-provided credentials)
    if is_nfs_path(dest_path):
        return _prepare_nfs_destination(job, backend_name, dest_path)

    # Local path, use as-is
    return dest_path, None


def _prepare_smb_destination(job: dict[str, Any], backend_name: str, dest_path: str) -> tuple[str, Any]:
    """Prepare SMB destination path for the backend."""
    server, share, subpath = parse_smb_path(dest_path)

    # Get credentials from job (passed by server)
    smb_username = job.get("smb_username")
    smb_password = job.get("smb_password")
    smb_domain = job.get("smb_domain")

    if backend_name == "kopia":
        # Kopia uses a mounted filesystem path for SMB on Linux.
        print(f"[SMB] Mounting share for {backend_name} backend")
        ctx = smb_mount_context(
            server=server,
            share=share,
            username=smb_username,
            password=smb_password,
            domain=smb_domain,
        )
        mount_path = ctx.__enter__()
        full_path = str(mount_path / subpath) if subpath else str(mount_path)
        print(f"[SMB] Using mounted path: {full_path}")
        return full_path, ctx

    # Unknown backend, try using path as-is
    print(f"[SMB] Warning: Unknown backend '{backend_name}', using path as-is")
    return dest_path, None


def _prepare_nfs_destination(job: dict[str, Any], backend_name: str, dest_path: str) -> tuple[str, Any]:
    """Prepare NFS destination path for the backend."""
    server, export_path, subpath = parse_nfs_path(dest_path)

    if backend_name == "kopia":
        # Kopia needs a mounted filesystem path.
        print(f"[NFS] Mounting NFS export for {backend_name} backend")
        ctx = nfs_mount_context(server=server, export_path=export_path)
        mount_path = ctx.__enter__()
        full_path = str(mount_path / subpath) if subpath else str(mount_path)
        print(f"[NFS] Using mounted path: {full_path}")
        return full_path, ctx

    # Unknown backend, try mounting anyway
    print(f"[NFS] Warning: Unknown backend '{backend_name}', mounting NFS export")
    ctx = nfs_mount_context(server=server, export_path=export_path)
    mount_path = ctx.__enter__()
    full_path = str(mount_path / subpath) if subpath else str(mount_path)
    return full_path, ctx


def prepare_source(job: dict[str, Any], backend_name: str) -> tuple[str, Any]:
    """Prepare source path for the backend, mounting SMB/NFS if needed."""
    source_path = job.get("source_path", "")

    # Proxy backend uses source_path as a proxy:// URI directly
    # Don't try to mount it as a filesystem path
    if backend_name == "proxy" or source_path.startswith(("proxy://", "proxys://")):
        print(f"[RESTORE] Using proxy URI directly: {source_path}")
        return source_path, None

    # Windows can use UNC paths directly, but does not support the NFS
    # source format used by the agent protocol.
    if sys.platform == "win32":
        if is_nfs_path(source_path) or (job.get("nfs_server") and job.get("nfs_export")):
            raise RuntimeError("NFS restores are not supported on Windows")
        _prepare_windows_smb(source_path, job)
        return source_path, None

    # Check if NFS credentials were passed (job linked to NFS repository)
    # This takes priority over parsing source_path as NFS, because the server
    # provides the actual NFS export separately from the subpath
    nfs_server = job.get("nfs_server")
    nfs_export = job.get("nfs_export")
    if nfs_server and nfs_export:
        # Server provided NFS export - mount the export and calculate subpath
        # source_path will be like: server:/export/path/Agents/jobname
        # nfs_export is the actual mountable export: /export/path
        # We need to extract the subpath after the export
        print(f"[RESTORE] Using NFS repository: {nfs_server}:{nfs_export}")

        # Calculate subpath: everything in source_path after the export
        subpath = ""
        if is_nfs_path(source_path):
            # Parse the full source to get the path portion
            _, full_path, _ = parse_nfs_path(source_path)
            # Remove the export prefix to get subpath
            if full_path.startswith(nfs_export):
                subpath = full_path[len(nfs_export) :].lstrip("/")
            else:
                # Export doesn't match - maybe path format differs, use full path as subpath
                subpath = full_path.lstrip("/")

        ctx = nfs_mount_context(server=nfs_server, export_path=nfs_export)
        mount_path = ctx.__enter__()
        full_path = str(mount_path / subpath) if subpath else str(mount_path)
        print(f"[RESTORE] Using mounted path: {full_path}")
        return full_path, ctx

    # Check for NFS path (server:/export format) without explicit credentials
    if is_nfs_path(source_path):
        print(f"[RESTORE] Detected NFS source path: {source_path}")
        return _prepare_nfs_source(job, backend_name, source_path)

    # Check for SMB path (//server/share or \\server\share format)
    if is_smb_path(source_path):
        print(f"[RESTORE] Detected SMB source path: {source_path}")
        return _prepare_smb_source(job, backend_name, source_path)

    # Local path, use as-is
    return source_path, None


def _prepare_windows_smb(path: str, job: dict[str, Any]) -> None:
    """Open the SMB session once before either backup or restore uses a UNC path."""
    global _smb_manager

    if not is_smb_path(path):
        return
    server, share, _ = parse_smb_path(path)
    if _smb_manager is None:
        from backer.agent.service import SMBConnectionManager

        _smb_manager = SMBConnectionManager()
    if not _smb_manager.connect(
        server, share, job.get("smb_username"), job.get("smb_password"), job.get("smb_domain")
    ):
        raise RuntimeError(f"Failed to connect to SMB share: \\{server}\\{share}")


def _prepare_smb_source(job: dict[str, Any], backend_name: str, source_path: str) -> tuple[str, Any]:
    """Prepare SMB source path for restore."""
    server, share, subpath = parse_smb_path(source_path)

    # Get credentials from job (passed by server)
    smb_username = job.get("smb_username")
    smb_password = job.get("smb_password")
    smb_domain = job.get("smb_domain")

    if backend_name == "kopia":
        # Kopia needs a mounted filesystem path.
        print(f"[RESTORE] Mounting SMB share for {backend_name} backend")
        ctx = smb_mount_context(
            server=server,
            share=share,
            username=smb_username,
            password=smb_password,
            domain=smb_domain,
        )
        mount_path = ctx.__enter__()
        full_path = str(mount_path / subpath) if subpath else str(mount_path)
        print(f"[RESTORE] Using mounted path: {full_path}")
        return full_path, ctx

    print(f"[RESTORE] Warning: Unknown backend '{backend_name}', using path as-is")
    return source_path, None


def _prepare_nfs_source(job: dict[str, Any], backend_name: str, source_path: str) -> tuple[str, Any]:
    """Prepare NFS source path for restore."""
    server, export_path, subpath = parse_nfs_path(source_path)

    # All backends need mounted path for NFS
    print(f"[RESTORE] Mounting NFS export {server}:{export_path} for {backend_name} backend")
    ctx = nfs_mount_context(server=server, export_path=export_path)
    mount_path = ctx.__enter__()
    full_path = str(mount_path / subpath) if subpath else str(mount_path)
    print(f"[RESTORE] Using mounted path: {full_path}")
    return full_path, ctx
