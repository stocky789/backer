"""Server-independent backup and restore runner."""

import json
import logging
import platform
import shutil
import socket
import sys
import tempfile
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from backer.backends import get_backend
from backer.backends.base import BackupDestination, BackupSource
from backer.core import mounts
from backer.core.destination import prepare_destination, prepare_source
from backer.core.paths import get_job_subfolder
from backer.core.repo_metadata import RepositoryMetadata

logger = logging.getLogger(__name__)

ProgressCallback = Callable[..., None]
ResultCallback = Callable[[dict[str, Any]], None]

_SENSITIVE_OPTION_PARTS = (
    "password",
    "token",
    "secret",
    "access_key",
    "api_key",
    "private_key",
    "authorization",
    "credential",
    "proxy_capability",
)


def _redact_repository_options(value: Any) -> Any:
    """Return a safe-to-log copy of backend options."""
    if isinstance(value, dict):
        return {
            key: "***"
            if any(part in str(key).lower() for part in _SENSITIVE_OPTION_PARTS)
            else _redact_repository_options(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_repository_options(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_repository_options(item) for item in value)
    return value


def _log_repository_options(operation: str, options: dict[str, Any]) -> None:
    print(f"[{operation}] Repository options: {_redact_repository_options(options)}")


def _backend_for_location(location: str, options: dict[str, Any]):
    lowered = location.lower()
    if lowered.startswith(("proxy://", "proxys://")):
        return get_backend("proxy", {**options, "location": location})
    if "://" in location and not lowered.startswith("s3://"):
        raise RuntimeError(f"Unsupported repository location: {location}")
    return get_backend("kopia", options)


def _progress(callback: ProgressCallback | None, **kwargs: Any) -> None:
    if callback:
        callback(**kwargs)


def run_backup(
    job: dict[str, Any],
    *,
    dry_run: bool = False,
    on_progress: ProgressCallback | None = None,
    on_result: ResultCallback | None = None,
    agent_credentials: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Run one backup without assuming an HTTP server exists."""
    run_id = job.get("run_id") or datetime.now().strftime("%Y%m%d_%H%M%S")
    started_at = datetime.now()
    job_name = job.get("job_name", "unknown")
    client_id = agent_credentials[0] if agent_credentials else None
    backend_name = "repository"
    print(f"[BACKUP] Starting job '{job_name}' with backend '{backend_name}'")
    print(f"[BACKUP] Source: {job.get('source_path')}")
    print(f"[BACKUP] Destination: {job.get('destination_path')}")
    _progress(on_progress, run_id=run_id, status="running", progress_percent=0, message="Initializing backup...")
    smb_cleanup_ctx = None
    try:
        repository_options = job.get("repository_options", {}).copy()
        if job.get("destination_path", "").lower().startswith(("proxy://", "proxys://")):
            proxy_id, proxy_secret = agent_credentials or (None, None)
            repository_options["client_id"] = proxy_id
            repository_options["client_secret"] = proxy_secret
        _log_repository_options("BACKUP", repository_options)
        backend = _backend_for_location(job.get("destination_path", ""), repository_options)
        is_proxy = job.get("destination_path", "").lower().startswith(("proxy://", "proxys://"))
        backend_name = "proxy" if is_proxy else "kopia"
        print("[BACKUP] Checking backend availability...")
        available, message = backend.check_available()
        if not available:
            print(f"[BACKUP] Backend not available: {message}")
            raise RuntimeError(f"Backend not available: {message}")
        print(f"[BACKUP] Backend ready: {message}")
        print(f"[BACKUP] Preparing destination path for {backend_name} backend...")
        dest_path, smb_cleanup_ctx = prepare_destination(job, backend_name)
        print(f"[BACKUP] Using destination: {dest_path}")
        _progress(
            on_progress,
            run_id=run_id,
            status="running",
            progress_percent=5,
            message="Backend ready, starting transfer...",
        )
        source = BackupSource(path=Path(job["source_path"]).expanduser(), excludes=job.get("excludes", []))
        destination = BackupDestination(path=dest_path)

        def progress_callback(
            bytes_done: int = 0, files_done: int = 0, current_file: str = "", total_bytes: int = 0
        ) -> None:
            percent = (
                5 + int((bytes_done / total_bytes) * 90)
                if total_bytes > 0
                else min(5 + files_done, 95)
                if files_done > 0
                else 5
            )
            _progress(
                on_progress,
                run_id=run_id,
                status="running",
                progress_percent=percent,
                current_file=current_file[:200] if current_file else None,
                bytes_processed=bytes_done,
                files_processed=files_done,
            )

        supports_progress = (
            hasattr(backend.backup, "__code__") and "progress_callback" in backend.backup.__code__.co_varnames
        )
        print(f"[BACKUP] Executing backup: {source.path} -> {dest_path}")
        result = backend.backup(
            source=source,
            destination=destination,
            dry_run=dry_run,
            progress_callback=progress_callback if supports_progress else None,
        )
        finished_at = datetime.now()
        if result.success:
            print(f"[BACKUP] Job '{job_name}' completed successfully")
            print(f"[BACKUP] Transferred: {result.bytes_transferred} bytes, {result.files_transferred} files")
        else:
            print(f"[BACKUP] Job '{job_name}' completed with errors")
            print(f"[BACKUP] Return code: {result.return_code}")
            if result.errors:
                print(f"[BACKUP] Errors: {result.errors[:5]}")
            if result.output:
                print(f"[BACKUP] Output (last 1000 chars): {result.output[-1000:]}")
        _progress(on_progress, run_id=run_id, status="finishing", progress_percent=95, message="Finalizing backup...")
        snapshot_id = result.metadata.get("snapshot_id") if hasattr(result, "metadata") and result.metadata else None
        if snapshot_id:
            print(f"[BACKUP] Captured snapshot ID: {snapshot_id}")
        report = {
            "run_id": run_id,
            "job_name": job_name,
            "client_id": client_id,
            "success": result.success,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "bytes_transferred": result.bytes_transferred,
            "files_transferred": result.files_transferred,
            "errors": result.errors,
            "output": result.output[:5000],
            "snapshot_id": snapshot_id,
        }
        try:
            if on_result:
                on_result(report)
        except Exception as e:
            print(f"Failed to report result: {e}")
        _write_repo_metadata(
            job, job.get("destination_path", ""), backend_name, result, started_at, finished_at, snapshot_id, client_id
        )
        return report
    except Exception as e:
        finished_at = datetime.now()
        error_msg = str(e)
        error_trace = traceback.format_exc()
        print(f"[BACKUP] Job '{job_name}' FAILED: {error_msg}")
        print(f"[BACKUP] Traceback:\n{error_trace}")
        _progress(on_progress, run_id=run_id, status="failed", progress_percent=0, message=error_msg[:200])
        report = {
            "run_id": run_id,
            "job_name": job_name,
            "client_id": client_id,
            "success": False,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "bytes_transferred": 0,
            "files_transferred": 0,
            "errors": [error_msg],
            "output": error_trace[:5000],
        }
        try:
            if on_result:
                on_result(report)
        except Exception as report_err:
            print(f"[BACKUP] Failed to report error to server: {report_err}")
        return report
    finally:
        if smb_cleanup_ctx is not None:
            try:
                smb_cleanup_ctx.__exit__(None, None, None)
            except Exception as cleanup_err:
                print(f"[SMB] Cleanup error: {cleanup_err}")


def run_restore(
    job: dict[str, Any],
    *,
    dry_run: bool = False,
    on_progress: ProgressCallback | None = None,
    on_result: ResultCallback | None = None,
    agent_credentials: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Run one restore without assuming an HTTP server exists."""
    run_id = job.get("run_id") or f"restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    started_at = datetime.now()
    job_name = job.get("job_name", "unknown")
    client_id = agent_credentials[0] if agent_credentials else None
    backend_name = "repository"
    print(f"[RESTORE] Starting restore for job '{job_name}' with backend '{backend_name}'")
    print(f"[RESTORE] Source (backup repo): {job.get('source_path')}")
    print(f"[RESTORE] Destination: {job.get('destination_path')}")
    _progress(on_progress, run_id=run_id, status="running", progress_percent=0, message="Initializing restore...")
    mount_cleanup_ctx = None
    try:
        repository_options = job.get("repository_options", {}).copy()
        if job.get("source_path", "").lower().startswith(("proxy://", "proxys://")):
            proxy_id, proxy_secret = agent_credentials or (None, None)
            repository_options["client_id"] = proxy_id
            repository_options["client_secret"] = proxy_secret
        _log_repository_options("RESTORE", repository_options)
        backend = _backend_for_location(job.get("source_path", ""), repository_options)
        is_proxy = job.get("source_path", "").lower().startswith(("proxy://", "proxys://"))
        backend_name = "proxy" if is_proxy else "kopia"
        print("[RESTORE] Checking backend availability...")
        available, message = backend.check_available()
        if not available:
            print(f"[RESTORE] Backend not available: {message}")
            raise RuntimeError(f"Backend not available: {message}")
        print(f"[RESTORE] Backend ready: {message}")
        print(f"[RESTORE] Preparing source path for {backend_name} backend...")
        source_path, mount_cleanup_ctx = prepare_source(job, backend_name)
        print(f"[RESTORE] Prepared source path: {source_path}")
        _progress(
            on_progress,
            run_id=run_id,
            status="running",
            progress_percent=5,
            message="Backend ready, starting restore...",
        )
        source = BackupDestination(path=source_path)
        destination = Path(job["destination_path"])
        clean_restore = job.get("clean_restore", False)
        restore_snapshot = job.get("snapshot")
        staged_destination: Path | None = None
        if clean_restore and not dry_run:
            if backend_name == "proxy":
                raise RuntimeError(
                    "Clean restore is not supported for proxy backends because the server cannot yet validate "
                    "a restore without modifying files"
                )
            if backend_name == "kopia":
                snapshots = backend.list_snapshots(source)
                selected = job.get("snapshot")
                if not snapshots or (
                    selected
                    and selected != "latest"
                    and not any(selected in (item.get("id"), item.get("full_id")) for item in snapshots)
                ):
                    raise RuntimeError("Clean restore requires an accessible Kopia repository and selected snapshot")
            else:
                validation = backend.restore(
                    source=source,
                    destination=destination,
                    snapshot=restore_snapshot,
                    dry_run=True,
                    original_source_path=job.get("original_source_path"),
                    include_path=job.get("source_subfolder") or None,
                )
                if not validation.success:
                    raise RuntimeError("Clean restore validation failed: " + "; ".join(validation.errors))
            resolved_destination = destination.resolve()
            if resolved_destination == resolved_destination.parent:
                raise RuntimeError("Clean restore refuses to replace a filesystem root")
            print(f"[RESTORE] Clean restore enabled - staging destination: {destination}")
            _progress(
                on_progress,
                run_id=run_id,
                status="running",
                progress_percent=3,
                message="Clean restore: staging existing files...",
            )
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    if destination.is_symlink() or not destination.is_dir():
                        raise RuntimeError("Clean restore destination must be a non-symlink directory")
                    destination_mode = destination.stat().st_mode & 0o7777
                    staged_destination = Path(tempfile.mkdtemp(prefix=".backer-restore-", dir=destination.parent))
                    staged_destination.rmdir()
                    destination.replace(staged_destination)
                    try:
                        destination.mkdir(mode=destination_mode)
                        destination.chmod(destination_mode)
                    except Exception as setup_err:
                        try:
                            if destination.exists():
                                destination.rmdir()
                            staged_destination.replace(destination)
                        except Exception as rollback_err:
                            raise RuntimeError(
                                "Clean restore setup rollback failed; original destination remains at "
                                f"{staged_destination}: {rollback_err}"
                            ) from rollback_err
                        raise RuntimeError(f"Clean restore failed to prepare destination: {setup_err}") from setup_err
                    print("[RESTORE] Staged destination directory contents")
                else:
                    destination.mkdir(parents=True, exist_ok=True)
                    print("[RESTORE] Created destination directory")
            except Exception as setup_err:
                raise RuntimeError(f"Clean restore failed to prepare destination: {setup_err}") from setup_err
        original_source_path = job.get("original_source_path")
        if original_source_path:
            print(f"[RESTORE] Original source path for snapshot lookup: {original_source_path}")
        try:
            result = backend.restore(
                source=source,
                destination=destination,
                snapshot=restore_snapshot,
                dry_run=dry_run,
                original_source_path=original_source_path,
                include_path=job.get("source_subfolder") or None,
            )
        except Exception:
            if clean_restore and not dry_run:
                try:
                    if destination.exists():
                        shutil.rmtree(
                            destination
                        ) if destination.is_dir() and not destination.is_symlink() else destination.unlink()
                    if staged_destination:
                        staged_destination.replace(destination)
                except Exception as rollback_err:
                    raise RuntimeError(
                        "Clean restore rollback failed; original destination remains at "
                        f"{staged_destination}: {rollback_err}"
                    ) from rollback_err
            raise
        if (
            clean_restore
            and not dry_run
            and result.success
            and (not destination.exists() or not any(p.is_file() for p in destination.rglob("*")))
        ):
            result.success = False
            result.errors.append(
                "Clean restore produced no files; keeping original destination"
                if staged_destination
                else "Clean restore produced no files"
            )
        if clean_restore and not dry_run and not result.success:
            try:
                if destination.exists():
                    shutil.rmtree(
                        destination
                    ) if destination.is_dir() and not destination.is_symlink() else destination.unlink()
                if staged_destination:
                    staged_destination.replace(destination)
            except Exception as rollback_err:
                result.errors.append(
                    "Clean restore rollback failed; original destination remains at "
                    f"{staged_destination}: {rollback_err}"
                )
        elif staged_destination:
            try:
                shutil.rmtree(staged_destination)
            except Exception as cleanup_err:
                result.warnings.append(
                    f"Clean restore succeeded but could not remove staged files at {staged_destination}: {cleanup_err}"
                )
        finished_at = datetime.now()
        _progress(on_progress, run_id=run_id, status="finishing", progress_percent=95, message="Finalizing restore...")
        output = getattr(result, "output", "")
        if result.warnings:
            output = "\n".join((output, *(f"WARNING: {warning}" for warning in result.warnings)))
        report = {
            "run_id": run_id,
            "job_name": f"restore:{job_name}",
            "client_id": client_id,
            "success": result.success,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "bytes_transferred": getattr(result, "bytes_transferred", 0),
            "files_transferred": result.files_transferred,
            "errors": result.errors,
            "output": output[:5000],
        }
        try:
            if on_result:
                on_result(report)
        except Exception as e:
            print(f"Failed to report restore result: {e}")
        if result.success and backend_name != "proxy":
            try:
                _write_restore_metadata(
                    source_path, job_name, run_id, result, started_at, finished_at, job.get("snapshot"), client_id
                )
            except Exception as meta_err:
                print(f"[RESTORE] Warning - failed to write restore metadata: {meta_err}")
        return report
    except Exception as e:
        finished_at = datetime.now()
        _progress(on_progress, run_id=run_id, status="failed", progress_percent=0, message=str(e)[:200])
        report = {
            "run_id": run_id,
            "job_name": f"restore:{job_name}",
            "client_id": client_id,
            "success": False,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "bytes_transferred": 0,
            "files_transferred": 0,
            "errors": [str(e)],
            "output": "",
        }
        try:
            if on_result:
                on_result(report)
        except Exception:
            pass
        return report
    finally:
        if mount_cleanup_ctx:
            try:
                print("[RESTORE] Cleaning up mounted path...")
                mount_cleanup_ctx.__exit__(None, None, None)
                print("[RESTORE] Mount cleanup complete")
            except Exception as cleanup_err:
                print(f"[RESTORE] Cleanup error: {cleanup_err}")


def _write_repo_metadata(
    job: dict[str, Any],
    dest_path: str,
    backend_name: str,
    result: Any,
    started_at: datetime,
    finished_at: datetime,
    snapshot_id: str | None,
    agent_id: str | None,
) -> None:
    try:
        print(f"[METADATA] Writing metadata to repository: {dest_path}")
        job_name, run_id, source_path = (
            job.get("job_name", "unknown"),
            job.get("run_id", "unknown"),
            job.get("source_path", ""),
        )
        if job.get("serverless") and job.get("repository_hint", {}).get("type") == "s3":
            from backer.serverless.s3_sidecar import S3Sidecar
            from backer.serverless.sidecar import build_serverless_job_document

            credentials = job.get("repository_options", {}).get("s3", {})
            sidecar = S3Sidecar(job["repository_hint"], credentials)
            job_key = f".backer/jobs/{get_job_subfolder(job_name)}/config.json"
            existing = sidecar.get(job_key)
            current = json.loads(existing) if existing else {}
            document = build_serverless_job_document(job, job_name, source_path, agent_id, current)
            sidecar.put_atomic(job_key, json.dumps(document).encode())
            return
        if sys.platform != "win32" and mounts.is_smb_path(dest_path):
            server, share, subpath = mounts.parse_smb_path(dest_path)
            print(f"[METADATA] Mounting SMB share //{server}/{share} for metadata")
            with mounts.smb_mount_context(
                server, share, job.get("smb_username"), job.get("smb_password"), job.get("smb_domain")
            ) as mount_point:
                _write_metadata_to_path(
                    mount_point / subpath if subpath else mount_point,
                    job_name,
                    run_id,
                    source_path,
                    backend_name,
                    result,
                    started_at,
                    finished_at,
                    snapshot_id,
                    agent_id,
                    job if job.get("serverless") else None,
                )
        else:
            _write_metadata_to_path(
                Path(dest_path),
                job_name,
                run_id,
                source_path,
                backend_name,
                result,
                started_at,
                finished_at,
                snapshot_id,
                agent_id,
                job if job.get("serverless") else None,
            )
        print(f"[METADATA] Successfully wrote metadata to: {dest_path}")
    except Exception as e:
        logger.error("[METADATA] Failed to write metadata to %s: %s", dest_path, e)


def _write_metadata_to_path(
    repo_path: Path,
    job_name: str,
    run_id: str,
    source_path: str,
    backend_name: str,
    result: Any,
    started_at: datetime,
    finished_at: datetime,
    snapshot_id: str | None,
    agent_id: str | None,
    serverless_job: dict[str, Any] | None = None,
) -> None:
    repo = RepositoryMetadata(repo_path)
    if not repo.is_initialized():
        print(f"[METADATA] Initializing metadata directory at {repo.metadata_dir}")
        repo.initialize()
    repo.save_agent(
        agent_id=agent_id,
        agent_data={
            "hostname": socket.gethostname(),
            "platform": sys.platform,
            "os_info": f"{platform.system()} {platform.release()}",
        },
    )
    if serverless_job:
        from backer.serverless.sidecar import build_serverless_job_document

        path = repo_path / ".backer" / "jobs" / get_job_subfolder(job_name) / "config.json"
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        from backer.serverless.sidecar import _write_json

        _write_json(path, build_serverless_job_document(serverless_job, job_name, source_path, agent_id, current))
    else:
        repo.save_job(job_name=job_name, job_config={"source_path": source_path, "client_id": agent_id})
    repo.save_job_run(
        job_name,
        run_id,
        {
            "status": "success" if result.success else "failed",
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "bytes_transferred": result.bytes_transferred,
            "files_transferred": result.files_transferred,
            "errors": result.errors,
            "snapshot_id": snapshot_id,
            "agent_id": agent_id,
            "hostname": socket.gethostname(),
        },
    )
    if snapshot_id and backend_name == "kopia":
        repo.save_snapshot(
            snapshot_id=snapshot_id,
            snapshot_data={
                "job_name": job_name,
                "run_id": run_id,
                "hostname": socket.gethostname(),
                "paths": [source_path],
                "time": finished_at.isoformat(),
            },
        )


def _write_restore_metadata(
    source_path: str,
    job_name: str,
    run_id: str,
    result: Any,
    started_at: datetime,
    finished_at: datetime,
    snapshot: str | None,
    agent_id: str | None,
) -> None:
    try:
        print(f"[RESTORE METADATA] Writing restore metadata to: {source_path}")
        if sys.platform != "win32" and mounts.is_smb_path(source_path):
            print("[RESTORE METADATA] Skipping SMB metadata write (not supported)")
            return
        repo = RepositoryMetadata(Path(source_path))
        if not repo.is_initialized():
            print("[RESTORE METADATA] Repository metadata not initialized, skipping")
            return
        repo.save_job_run(
            job_name,
            run_id,
            {
                "operation_type": "restore",
                "status": "success" if result.success else "failed",
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "bytes_transferred": getattr(result, "bytes_transferred", 0),
                "files_transferred": result.files_transferred,
                "snapshot_id": snapshot,
                "agent_id": agent_id,
                "hostname": socket.gethostname(),
            },
        )
        print(f"[RESTORE METADATA] Successfully wrote restore metadata for job '{job_name}'")
    except Exception as e:
        print(f"[RESTORE METADATA] Warning - failed to write metadata: {e}")
