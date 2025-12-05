"""FastAPI application for the Backer server."""

import hashlib
import json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from backer import __version__
from backer.server.models import (
    BackupResult,
    Client,
    ClientHeartbeat,
    ClientRegisterRequest,
    ClientRegisterResponse,
    ClientStatus,
    JobCreate,
    JobResponse,
    JobRunRequest,
    JobRunResponse,
)
from backer.server.scheduler import BackupScheduler
from backer.server.storage import Storage
from backer.server.tasks import Task, get_task_manager
from backer.server.web.routes import router as web_router

logger = logging.getLogger(__name__)

# Characters not allowed in job names (prevent path traversal and shell injection)
_UNSAFE_NAME_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]|\.\.|\.\/')


def validate_name(name: str, field: str = "name") -> None:
    """Validate a name field to prevent path traversal and injection.

    Raises HTTPException if invalid.
    """
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail=f"{field} cannot be empty")
    if len(name) > 255:
        raise HTTPException(status_code=400, detail=f"{field} too long (max 255 chars)")
    if _UNSAFE_NAME_PATTERN.search(name):
        raise HTTPException(
            status_code=400,
            detail=f"{field} contains invalid characters (no path separators, quotes, or special chars)",
        )


# Global storage instance (initialized in create_app)
_storage: Storage | None = None
_scheduler: BackupScheduler | None = None

security = HTTPBasic(auto_error=False)


def get_storage() -> Storage:
    """Get the storage instance."""
    if _storage is None:
        raise RuntimeError("Storage not initialized")
    return _storage


def _build_backup_command_payload(
    job: dict[str, Any],
    job_name: str,
    run_id: str,
    dry_run: bool = False,
    storage: Storage | None = None,
) -> dict[str, Any]:
    """Build the backup command payload with repository credentials.

    This ensures SMB credentials are included for Linux agents that
    need them to access network shares, and repository passwords are
    included for restic/kopia backends.
    """
    storage = storage or _storage
    if storage is None:
        raise RuntimeError("Storage not initialized")

    # Start with basic payload
    backend_options = job.get("backend_options", {}).copy()
    backend = job.get("backend", "rclone")

    payload = {
        "job_name": job_name,
        "run_id": run_id,
        "source_path": job.get("source_path"),
        "destination_path": job.get("destination_path"),
        "backend": backend,
        "excludes": job.get("excludes", []),
        "backend_options": backend_options,
        "dry_run": dry_run,
    }

    # If job has a repository, include credentials for agents
    repository_id = job.get("repository_id")
    if repository_id:
        repo = storage.get_repository(repository_id)
        if repo:
            repo_type = repo.get("repo_type", "")

            # Get decrypted repository password
            repo_password = storage.get_repository_password(repository_id)

            if repo_type == "smb":
                # Include SMB connection info for agents
                payload["smb_server"] = repo.get("server")
                payload["smb_share"] = repo.get("share")
                payload["smb_username"] = repo.get("username")
                payload["smb_domain"] = repo.get("domain")
                if repo_password:
                    payload["smb_password"] = repo_password

            elif repo_type == "nfs":
                # NFS info (no password needed typically)
                payload["nfs_server"] = repo.get("server")
                payload["nfs_export"] = repo.get("share")

            # For restic/kopia backends, include the repository password
            # This is needed for the agent to authenticate with the backup repository
            if backend in ("restic", "kopia") and repo_password:
                # Only set if not already provided in backend_options
                if "password" not in backend_options:
                    payload["backend_options"]["password"] = repo_password
                    logger.debug(f"[BACKUP] Added repository password to backend_options for {backend} backend")

    # Also check if password is directly in job's backend_options (legacy/manual config)
    # This handles cases where the password was set directly on the job config
    if backend in ("restic", "kopia"):
        if "password" not in payload["backend_options"]:
            # Check for password in job's backend_options
            job_password = job.get("backend_options", {}).get("password")
            if job_password:
                payload["backend_options"]["password"] = job_password

    return payload


def trigger_job_internal(job_name: str) -> None:
    """Internal function to trigger a job (used by scheduler).

    This bypasses HTTP and directly queues the backup command.
    """
    if _storage is None:
        logger.error("Storage not initialized")
        return

    job = _storage.get_job(job_name)
    if not job:
        logger.error(f"Job not found: {job_name}")
        return

    client_id = job.get("client_id")
    if not client_id:
        logger.error(f"Job {job_name} has no assigned agent")
        return

    client = _storage.get_client(client_id)
    if not client:
        logger.error(f"Agent {client_id} not found for job {job_name}")
        return

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    started_at = datetime.now()

    # Save the run record as "pending"
    _storage.save_job_run(
        run_id=run_id,
        job_name=job_name,
        status="pending",
        started_at=started_at,
        client_id=client_id,
    )

    # Start progress tracking
    _storage.start_job_progress(
        run_id=run_id,
        job_name=job_name,
        client_id=client_id,
    )

    # Build and queue the backup command with repository credentials
    command_payload = _build_backup_command_payload(
        job=job,
        job_name=job_name,
        run_id=run_id,
        dry_run=False,
    )

    _storage.queue_command(
        client_id=client_id,
        command_type="backup",
        payload=command_payload,
    )

    logger.info(f"Scheduled job {job_name} queued for agent {client_id} (run_id: {run_id})")


def verify_client(
    credentials: HTTPBasicCredentials | None = Depends(security),
    storage: Storage = Depends(get_storage),
) -> Client:
    """Verify client credentials and return client info."""
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    client = storage.get_client(credentials.username)
    if not client:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid client ID")

    secret_hash = storage.get_client_secret_hash(credentials.username)
    provided_hash = hashlib.sha256(credentials.password.encode()).hexdigest()

    if not secrets.compare_digest(secret_hash or "", provided_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    return client


def create_app(data_dir: Path | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    global _storage, _scheduler

    if data_dir is None:
        data_dir = Path.home() / ".local" / "share" / "backer"

    _storage = Storage(data_dir / "backer.db")

    # Initialize scheduler
    _scheduler = BackupScheduler(
        storage=_storage,
        job_trigger_callback=trigger_job_internal,
        check_interval=60,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        logger.info("Starting Backer server...")
        if _scheduler:
            _scheduler.start()
        yield
        # Shutdown
        logger.info("Shutting down Backer server...")
        if _scheduler:
            _scheduler.stop()

    app = FastAPI(
        title="Backer Server",
        description="Centralized backup management server",
        version=__version__,
        lifespan=lifespan,
    )

    # CORS configuration
    # Note: allow_credentials=True with allow_origins=["*"] is insecure
    # For local deployments, we allow all origins but without credentials
    # If you need cross-origin requests with credentials, specify exact origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,  # Credentials require specific origins, not wildcard
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Global exception handler for malformed JSON requests
    @app.exception_handler(json.JSONDecodeError)
    async def json_decode_error_handler(
        request: Request, exc: json.JSONDecodeError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Invalid JSON in request body: {exc.msg}"},
        )

    # Health check
    @app.get("/health")
    def health_check() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    # ============ Background Tasks ============

    @app.get("/api/v1/tasks")
    def list_tasks(active_only: bool = False) -> list[dict[str, Any]]:
        """List background tasks.

        Args:
            active_only: If true, only return pending/running tasks
        """
        task_manager = get_task_manager()
        if active_only:
            tasks = task_manager.get_active_tasks()
        else:
            tasks = task_manager.get_recent_tasks(limit=20)
        return [t.to_dict() for t in tasks]

    @app.get("/api/v1/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        """Get status of a specific task."""
        task_manager = get_task_manager()
        task = task_manager.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return task.to_dict()

    # ============ Client Management ============

    @app.post("/api/v1/clients/register", response_model=ClientRegisterResponse)
    def register_client(
        request: ClientRegisterRequest, req: Request, storage: Storage = Depends(get_storage)
    ) -> ClientRegisterResponse:
        """Register a new client or re-register existing one by hostname."""
        # Check if client with this hostname already exists
        existing_client = storage.get_client_by_hostname(request.hostname)

        if existing_client:
            # Re-register: generate new secret for existing client
            client_secret = secrets.token_urlsafe(32)
            secret_hash = hashlib.sha256(client_secret.encode()).hexdigest()
            storage.update_client_secret(existing_client.id, secret_hash)

            # Update client status to online
            storage.update_client_status(
                existing_client.id,
                ClientStatus.ONLINE,
                ip_address=req.client.host if req.client else None,
            )

            logger.info(f"Re-registered existing client: {existing_client.id} ({request.hostname})")

            return ClientRegisterResponse(
                client_id=existing_client.id,
                client_secret=client_secret,
                server_version=__version__,
            )

        # New client registration
        client_id = str(uuid4())[:8]
        client_secret = secrets.token_urlsafe(32)
        secret_hash = hashlib.sha256(client_secret.encode()).hexdigest()

        client = Client(
            id=client_id,
            name=request.hostname,
            hostname=request.hostname,
            ip_address=req.client.host if req.client else None,
            status=ClientStatus.ONLINE,
            last_seen=datetime.now(),
            version=request.version,
            os_info=request.os_info,
            tags=request.tags,
        )

        storage.add_client(client, secret_hash)
        logger.info(f"Registered new client: {client_id} ({request.hostname})")

        return ClientRegisterResponse(
            client_id=client_id,
            client_secret=client_secret,
            server_version=__version__,
        )

    @app.get("/api/v1/clients", response_model=list[Client])
    def list_clients(storage: Storage = Depends(get_storage)) -> list[Client]:
        """List all registered clients."""
        return storage.list_clients()

    @app.get("/api/v1/clients/{client_id}", response_model=Client)
    def get_client(client_id: str, storage: Storage = Depends(get_storage)) -> Client:
        """Get a specific client by ID."""
        client = storage.get_client(client_id)
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")
        return client

    @app.delete("/api/v1/clients/{client_id}")
    def delete_client(
        client_id: str,
        delete_metadata: bool = True,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Remove a client and optionally clean up repository metadata.

        The client is removed from the database immediately.
        Metadata cleanup from repositories runs in the background.

        Args:
            client_id: The client ID to delete
            delete_metadata: If True (default), also delete agent metadata from all repositories
        """
        # Get client info before deleting (for hostname matching)
        client = storage.get_client(client_id)
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")

        client_name = client.get("name", client_id)

        # Delete client from database immediately
        if not storage.delete_client(client_id):
            raise HTTPException(status_code=404, detail="Client not found")

        result: dict[str, Any] = {"status": "deleted"}

        # Schedule metadata cleanup as a background task
        if delete_metadata:
            # Capture repository info for the background task
            repos = storage.list_repositories()
            repo_info = []
            for repo in repos:
                repo_id = repo.get("id")
                repo_info.append({
                    "repo_id": repo_id,
                    "repo_type": repo.get("repo_type", "smb"),
                    "server": repo.get("server", ""),
                    "share": repo.get("share", ""),
                    "path": repo.get("path", ""),
                    "username": repo.get("username"),
                    "password": storage.get_repository_password(repo_id) if repo_id else None,
                    "domain": repo.get("domain"),
                })

            def cleanup_agent_metadata(task: Task) -> dict[str, Any]:
                """Background task to clean up agent metadata from repositories."""
                from backer.server.repositories import smb_delete_file

                metadata_deleted = 0
                metadata_errors = []
                total = len(repo_info)

                for i, repo in enumerate(repo_info):
                    task.progress = int((i / total) * 100) if total > 0 else 0
                    task.message = f"Cleaning up repository {i + 1} of {total}..."

                    # Build path to agent metadata file
                    subpath = repo["path"]
                    metadata_base = f"{subpath}/.backer" if subpath else ".backer"
                    agent_path = f"{metadata_base}/agents/{client_id}.json"

                    try:
                        if repo["repo_type"] == "smb":
                            success, msg = smb_delete_file(
                                repo["server"], repo["share"], agent_path,
                                repo["username"], repo["password"], repo["domain"]
                            )
                            if success:
                                metadata_deleted += 1
                                logger.info(
                                    f"[DELETE AGENT] Deleted metadata from "
                                    f"{repo['server']}/{repo['share']}: {agent_path}"
                                )
                            elif "already deleted" not in msg.lower() and "no such file" not in msg.lower():
                                metadata_errors.append(f"{repo['server']}/{repo['share']}: {msg}")
                    except Exception as e:
                        metadata_errors.append(f"{repo['server']}/{repo['share']}: {str(e)}")
                        logger.warning(f"[DELETE AGENT] Error: {e}")

                return {"deleted": metadata_deleted, "errors": metadata_errors}

            task_manager = get_task_manager()
            task = task_manager.submit_with_func(
                task_type="delete_agent_metadata",
                description=f"Cleaning up metadata for agent '{client_name}'",
                func=cleanup_agent_metadata,
            )
            result["cleanup_task_id"] = task.id
            result["message"] = "Agent deleted. Metadata cleanup running in background."

        return result

    @app.post("/api/v1/commands/clear")
    def clear_pending_commands(
        storage: Storage = Depends(get_storage),
        client_id: str | None = None,
    ) -> dict[str, Any]:
        """Clear all pending commands from the queue.

        Optionally filter by client_id to only clear commands for a specific agent.
        This is useful for clearing stuck restore/backup commands.
        """
        count = storage.clear_pending_commands(client_id)
        return {
            "status": "ok",
            "cleared": count,
            "client_id": client_id,
        }

    @app.post("/api/v1/clients/heartbeat")
    async def client_heartbeat(
        heartbeat: ClientHeartbeat,
        req: Request,
        client: Client = Depends(verify_client),
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Receive heartbeat from a client.

        Uses long-polling: if no commands are pending, waits up to 25 seconds
        for a command to arrive before returning. This gives near-instant
        command delivery for interactive operations like file browsing.
        """
        import asyncio

        storage.update_client_status(
            client.id, ClientStatus.ONLINE, ip_address=req.client.host if req.client else None
        )

        # Check for pending commands immediately
        commands = storage.get_pending_commands(client.id)
        if commands:
            logger.info(f"[HEARTBEAT] Agent '{client.id}' receiving {len(commands)} command(s)")
            for cmd in commands:
                logger.debug(f"[HEARTBEAT] Command: {cmd}")
            return {"status": "ok", "commands": commands}

        # Long-polling: wait up to 25 seconds for a command to arrive
        # Check every 0.5 seconds for new commands
        for _ in range(50):  # 50 * 0.5s = 25 seconds max wait
            await asyncio.sleep(0.5)
            commands = storage.get_pending_commands(client.id)
            if commands:
                logger.info(f"[HEARTBEAT] Agent '{client.id}' receiving {len(commands)} command(s) (long-poll)")
                for cmd in commands:
                    logger.debug(f"[HEARTBEAT] Command: {cmd}")
                return {"status": "ok", "commands": commands}

        # No commands after timeout - return empty
        return {"status": "ok", "commands": []}

    # ============ Job Management ============

    @app.post("/api/v1/jobs", response_model=JobResponse)
    def create_job(job: JobCreate, storage: Storage = Depends(get_storage)) -> JobResponse:
        """Create a new backup job."""
        # Validate job name for security (path traversal, special chars)
        validate_name(job.name, "Job name")
        if job.name.lower().startswith("restore:"):
            raise HTTPException(status_code=400, detail="Job name cannot start with 'restore:'")

        if storage.get_job(job.name):
            raise HTTPException(status_code=409, detail="Job already exists")

        config = job.model_dump()
        storage.save_job(job.name, config)

        return JobResponse(
            name=job.name,
            source_path=job.source_path,
            destination_path=job.destination_path,
            backend=job.backend,
            client_id=job.client_id,
            enabled=job.enabled,
            schedule_cron=job.schedule_cron,
            last_run=None,
            last_status=None,
            next_run=None,
        )

    @app.get("/api/v1/jobs", response_model=list[JobResponse])
    def list_jobs(storage: Storage = Depends(get_storage)) -> list[JobResponse]:
        """List all backup jobs."""
        jobs = storage.list_jobs()
        responses = []
        for job in jobs:
            latest = storage.get_latest_run(job["name"])

            # Get next run from scheduler
            next_run = None
            schedule = job.get("schedule_cron")
            if schedule and job.get("enabled", True) and _scheduler:
                next_run = _scheduler.get_next_run(schedule)

            responses.append(
                JobResponse(
                    name=job["name"],
                    source_path=job.get("source_path", ""),
                    destination_path=job.get("destination_path", ""),
                    backend=job.get("backend", "rclone"),
                    client_id=job.get("client_id"),
                    enabled=job.get("enabled", True),
                    schedule_cron=job.get("schedule_cron"),
                    last_run=datetime.fromisoformat(latest["started_at"]) if latest else None,
                    last_status=latest["status"] if latest else None,
                    next_run=next_run,
                )
            )
        return responses

    @app.get("/api/v1/jobs/{job_name}")
    def get_job(job_name: str, storage: Storage = Depends(get_storage)) -> dict[str, Any]:
        """Get a specific job."""
        job = storage.get_job(job_name)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return {"name": job_name, **job}

    @app.put("/api/v1/jobs/{job_name}")
    async def update_job(
        job_name: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Update an existing job."""
        existing = storage.get_job(job_name)
        if not existing:
            raise HTTPException(status_code=404, detail="Job not found")

        data = await request.json()

        # Special handling for nested dicts that should be merged, not replaced
        # Merge backend_options if both exist and incoming is not empty
        # Empty dict {} means "clear all", non-empty means "merge"
        if "backend_options" in data:
            if data["backend_options"] and "backend_options" in existing:
                # Non-empty: merge with existing
                merged_backend_options = {**existing.get("backend_options", {}), **data["backend_options"]}
                data["backend_options"] = merged_backend_options
            # If data["backend_options"] is empty {}, it will replace existing (clearing it)

        # Merge retention if both exist and incoming is not empty
        if "retention" in data:
            if data["retention"] and "retention" in existing:
                merged_retention = {**existing.get("retention", {}), **data["retention"]}
                data["retention"] = merged_retention

        # Merge updates with existing config
        updated_config = {**existing, **data}
        storage.save_job(job_name, updated_config)

        return {"name": job_name, "status": "updated", **updated_config}

    @app.patch("/api/v1/jobs/{job_name}/toggle")
    def toggle_job(job_name: str, storage: Storage = Depends(get_storage)) -> dict[str, Any]:
        """Toggle job enabled/disabled status."""
        existing = storage.get_job(job_name)
        if not existing:
            raise HTTPException(status_code=404, detail="Job not found")

        existing["enabled"] = not existing.get("enabled", True)
        storage.save_job(job_name, existing)

        return {"name": job_name, "enabled": existing["enabled"]}

    @app.delete("/api/v1/jobs/{job_name}")
    def delete_job(
        job_name: str,
        delete_snapshots: bool = False,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Delete a job and its repository metadata.

        Args:
            job_name: Name of the job to delete
            delete_snapshots: If True, also delete associated snapshot metadata
        """
        import shutil
        from pathlib import Path as PathLib

        from backer.server.repositories import (
            smb_delete_directory,
            smb_delete_file,
            smb_list_files,
            smb_read_file,
        )

        # Get job config before deleting (to find repository)
        job = storage.get_job(job_name)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        logger.info(f"[DELETE JOB] Deleting job '{job_name}', delete_snapshots={delete_snapshots}")

        job_deleted = False
        snapshots_deleted = 0
        job_error = None
        snapshot_errors = []

        # Try to delete metadata from repository
        repo_id = job.get("repository_id")
        logger.info(f"[DELETE JOB] Job repo_id: {repo_id}")
        if repo_id:
            repo = storage.get_repository(repo_id)
            logger.info(f"[DELETE JOB] Found repo: {repo is not None}")
            if repo:
                repo_type = repo.get("repo_type", "smb")
                server = repo.get("server", "")
                share = repo.get("share", "")
                subpath = repo.get("path", "")
                username = repo.get("username")
                password = storage.get_repository_password(repo_id)
                domain = repo.get("domain")

                # Build paths
                metadata_base = f"{subpath}/.backer" if subpath else ".backer"
                job_path = f"{metadata_base}/jobs/{job_name}"
                snapshots_path = f"{metadata_base}/snapshots"

                try:
                    if repo_type == "smb":
                        # Delete job metadata using SMB
                        logger.info(f"[DELETE JOB] SMB delete: server={server}, share={share}, path={job_path}")
                        success, msg = smb_delete_directory(
                            server, share, job_path,
                            username, password, domain
                        )
                        logger.info(f"[DELETE JOB] SMB delete result: success={success}, msg={msg}")
                        if success:
                            job_deleted = True
                            logger.info(f"Deleted job metadata from SMB: {job_path} - {msg}")
                        else:
                            job_error = msg
                            logger.warning(f"Failed to delete job metadata from SMB: {msg}")

                        # Delete associated snapshots if requested
                        if delete_snapshots:
                            # Get the job's client hostname to match snapshots
                            client_id = job.get("client_id")
                            client_hostname = None
                            if client_id:
                                client = storage.get_client(client_id)
                                if client:
                                    client_hostname = client.hostname

                            job_source_path = job.get("source_path", "")
                            logger.info(
                                f"[DELETE SNAPSHOTS] Looking for snapshots: "
                                f"hostname={client_hostname}, source_path={job_source_path}"
                            )

                            # List and delete matching snapshots
                            ok, snap_files = smb_list_files(
                                server, share, snapshots_path,
                                username, password, domain
                            )
                            snap_count = len(snap_files) if ok else 0
                            logger.info(f"[DELETE SNAPSHOTS] Found {snap_count} files in {snapshots_path}")
                            if ok and snap_files:
                                for snap_file in snap_files:
                                    if not snap_file.endswith(".json"):
                                        continue

                                    # Read snapshot to check if it matches this job
                                    ok2, content = smb_read_file(
                                        server, share, f"{snapshots_path}/{snap_file}",
                                        username, password, domain
                                    )
                                    if ok2:
                                        import json as json_module
                                        try:
                                            snap_data = json_module.loads(content)
                                            # Match by hostname and check if paths overlap
                                            snap_hostname = snap_data.get("hostname", "")
                                            snap_paths = snap_data.get("paths", [])

                                            # Check if this snapshot could be from this job
                                            should_delete = False
                                            if client_hostname and snap_hostname == client_hostname:
                                                # Same host - check if paths match
                                                if job_source_path:
                                                    # Normalize paths for comparison
                                                    norm_job = job_source_path.replace("\\", "/").rstrip("/")
                                                    for sp in snap_paths:
                                                        norm_snap = sp.replace("\\", "/").rstrip("/")
                                                        if norm_job in norm_snap or norm_snap in norm_job:
                                                            should_delete = True
                                                            logger.info(
                                                                f"[DELETE SNAPSHOTS] Match: {snap_file}"
                                                            )
                                                            break
                                            else:
                                                pass  # No hostname match - skip silently

                                            if should_delete:
                                                del_ok, del_msg = smb_delete_file(
                                                    server, share, f"{snapshots_path}/{snap_file}",
                                                    username, password, domain
                                                )
                                                if del_ok:
                                                    snapshots_deleted += 1
                                                    logger.info(f"Deleted snapshot: {snap_file}")
                                                else:
                                                    snapshot_errors.append(f"{snap_file}: {del_msg}")

                                        except (json_module.JSONDecodeError, KeyError):
                                            pass

                    elif repo_type == "local" or repo.get("mount_point"):
                        # Direct filesystem access
                        if repo.get("mount_point"):
                            base_path = PathLib(repo["mount_point"])
                        else:
                            base_path = PathLib(share or repo.get("path", ""))

                        if subpath:
                            base_path = base_path / subpath

                        job_metadata_path = base_path / ".backer" / "jobs" / job_name
                        if job_metadata_path.exists():
                            shutil.rmtree(job_metadata_path)
                            job_deleted = True
                            logger.info(f"Deleted job metadata: {job_metadata_path}")

                        # Delete snapshots if requested
                        if delete_snapshots:
                            snapshots_dir = base_path / ".backer" / "snapshots"
                            if snapshots_dir.exists():
                                # Similar logic as SMB but for local files
                                client_id = job.get("client_id")
                                client_hostname = None
                                if client_id:
                                    client = storage.get_client(client_id)
                                    if client:
                                        client_hostname = client.hostname

                                import json as json_module
                                job_source_path = job.get("source_path", "")
                                for snap_file in snapshots_dir.glob("*.json"):
                                    try:
                                        snap_data = json_module.loads(snap_file.read_text())
                                        snap_hostname = snap_data.get("hostname", "")
                                        snap_paths = snap_data.get("paths", [])

                                        should_delete = False
                                        if client_hostname and snap_hostname == client_hostname:
                                            if job_source_path:
                                                norm_job = job_source_path.replace("\\", "/").rstrip("/")
                                                for sp in snap_paths:
                                                    norm_snap = sp.replace("\\", "/").rstrip("/")
                                                    if norm_job in norm_snap or norm_snap in norm_job:
                                                        should_delete = True
                                                        break

                                        if should_delete:
                                            snap_file.unlink()
                                            snapshots_deleted += 1
                                    except (json_module.JSONDecodeError, OSError):
                                        pass

                    # NFS - similar to local if mounted
                    elif repo_type == "nfs" and repo.get("mount_point"):
                        base_path = PathLib(repo["mount_point"])
                        if subpath:
                            base_path = base_path / subpath
                        job_metadata_path = base_path / ".backer" / "jobs" / job_name
                        if job_metadata_path.exists():
                            shutil.rmtree(job_metadata_path)
                            job_deleted = True

                except Exception as e:
                    job_error = str(e)
                    logger.exception(f"Error deleting job metadata: {e}")

        # Delete job from database
        if not storage.delete_job(job_name):
            raise HTTPException(status_code=404, detail="Job not found")

        result: dict[str, Any] = {"status": "deleted"}
        if job_deleted:
            result["metadata_deleted"] = True
        if snapshots_deleted > 0:
            result["snapshots_deleted"] = snapshots_deleted
        if job_error:
            result["metadata_warning"] = f"Job deleted but metadata cleanup failed: {job_error}"
        if snapshot_errors:
            result["snapshot_warnings"] = snapshot_errors[:5]  # Limit to first 5

        return result

    @app.post("/api/v1/jobs/{job_name}/run", response_model=JobRunResponse)
    def run_job(
        job_name: str,
        request: JobRunRequest | None = None,
        storage: Storage = Depends(get_storage),
    ) -> JobRunResponse:
        """Trigger a job to run."""
        # Default to non-dry-run if no request body provided
        dry_run = request.dry_run if request else False

        job = storage.get_job(job_name)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        client_id = job.get("client_id")
        if not client_id:
            raise HTTPException(
                status_code=400,
                detail="Job has no assigned agent. Assign an agent to run this job.",
            )

        # Check client exists
        client = storage.get_client(client_id)
        if not client:
            raise HTTPException(status_code=400, detail=f"Agent '{client_id}' not found")

        run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        started_at = datetime.now()

        # Save the run record as "pending"
        storage.save_job_run(
            run_id=run_id,
            job_name=job_name,
            status="pending",
            started_at=started_at,
            client_id=client_id,
        )

        # Start progress tracking
        storage.start_job_progress(
            run_id=run_id,
            job_name=job_name,
            client_id=client_id,
        )

        # Build and queue the backup command with repository credentials
        command_payload = _build_backup_command_payload(
            job=job,
            job_name=job_name,
            run_id=run_id,
            dry_run=dry_run,
            storage=storage,
        )

        storage.queue_command(
            client_id=client_id,
            command_type="backup",
            payload=command_payload,
        )

        logger.info(f"[JOB RUN] Job '{job_name}' queued for agent '{client_id}' (run_id: {run_id})")

        return JobRunResponse(
            run_id=run_id,
            job_name=job_name,
            status="pending",
            started_at=started_at,
            message=f"Backup queued for agent '{client_id}'" + (" (dry run)" if dry_run else ""),
        )

    @app.get("/api/v1/jobs/{job_name}/runs")
    def get_job_runs(
        job_name: str, limit: int = 20, storage: Storage = Depends(get_storage)
    ) -> list[dict[str, Any]]:
        """Get run history for a job."""
        if not storage.get_job(job_name):
            raise HTTPException(status_code=404, detail="Job not found")
        return storage.get_job_runs(job_name, limit)

    @app.get("/api/v1/runs/{run_id}")
    def get_run_details(
        run_id: str, storage: Storage = Depends(get_storage)
    ) -> dict[str, Any]:
        """Get details for a specific job run including logs and output."""
        run = storage.get_job_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        # Parse errors JSON if stored as string
        errors = run.get("errors", [])
        if isinstance(errors, str):
            try:
                errors = json.loads(errors)
            except json.JSONDecodeError:
                errors = [errors] if errors else []

        return {
            "run_id": run["run_id"],
            "job_name": run["job_name"],
            "status": run["status"],
            "client_id": run.get("client_id"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "bytes_transferred": run.get("bytes_transferred", 0),
            "files_transferred": run.get("files_transferred", 0),
            "errors": errors,
            "output": run.get("output", ""),
            "snapshot_id": run.get("snapshot_id"),
        }

    # ============ Command acknowledgement ============

    @app.post("/api/v1/commands/{command_id}/ack")
    def acknowledge_command(
        command_id: int,
        client: Client = Depends(verify_client),
        storage: Storage = Depends(get_storage),
    ) -> dict[str, str]:
        """Mark a command as received/executed by the client."""
        storage.mark_command_executed(command_id)
        return {"status": "acknowledged"}

    # ============ Client-reported results ============

    @app.post("/api/v1/results")
    def report_result(
        result: BackupResult,
        client: Client = Depends(verify_client),
        storage: Storage = Depends(get_storage),
    ) -> dict[str, str]:
        """Client reports backup result."""
        status = "success" if result.success else "failed"
        logger.info(f"[RESULT] Job '{result.job_name}' (run_id: {result.run_id}) completed with status: {status}")
        if result.errors:
            logger.warning(f"[RESULT] Job '{result.job_name}' errors: {result.errors}")
        if result.output:
            logger.debug(f"[RESULT] Job '{result.job_name}' output (first 500 chars): {result.output[:500]}")

        storage.save_job_run(
            run_id=result.run_id,
            job_name=result.job_name,
            status=status,
            started_at=result.started_at,
            finished_at=result.finished_at,
            client_id=result.client_id,
            bytes_transferred=result.bytes_transferred,
            files_transferred=result.files_transferred,
            errors=result.errors,
            output=result.output[:10000],  # Limit output size
            snapshot_id=result.snapshot_id,  # For restic backups
        )
        # Mark progress as complete
        storage.finish_job_progress(
            result.run_id,
            status="completed" if result.success else "failed"
        )
        return {"status": "recorded"}

    # ============ Progress Tracking ============

    @app.post("/api/v1/progress")
    async def report_progress(
        request: Request,
        client: Client = Depends(verify_client),
        storage: Storage = Depends(get_storage),
    ) -> dict[str, str]:
        """Client reports backup progress update."""
        data = await request.json()
        run_id = data.get("run_id")
        if not run_id:
            raise HTTPException(status_code=400, detail="run_id required")

        storage.update_job_progress(
            run_id=run_id,
            status=data.get("status"),
            progress_percent=data.get("progress_percent"),
            current_file=data.get("current_file"),
            bytes_processed=data.get("bytes_processed"),
            files_processed=data.get("files_processed"),
            total_bytes=data.get("total_bytes"),
            total_files=data.get("total_files"),
            message=data.get("message"),
        )
        return {"status": "updated"}

    @app.get("/api/v1/running")
    def get_running_jobs(storage: Storage = Depends(get_storage)) -> list[dict[str, Any]]:
        """Get all currently running backup jobs."""
        return storage.get_running_jobs()

    @app.get("/api/v1/progress/{run_id}")
    def get_progress(run_id: str, storage: Storage = Depends(get_storage)) -> dict[str, Any]:
        """Get progress for a specific job run."""
        progress = storage.get_job_progress(run_id)
        if not progress:
            raise HTTPException(status_code=404, detail="Run not found")
        return progress

    # ============ Agent Filesystem Browsing ============

    @app.post("/api/v1/agents/{client_id}/browse")
    async def browse_agent_filesystem(
        client_id: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Request filesystem listing from an agent.

        Body:
        - path: Directory path to browse (optional, defaults to root/home)
        """
        client = storage.get_client(client_id)
        if not client:
            raise HTTPException(status_code=404, detail="Agent not found")

        try:
            data = await request.json()
        except Exception:
            data = {}
        path = data.get("path", "")

        # Generate a unique request ID
        request_id = f"browse_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"

        # Queue the browse command for the agent
        storage.queue_command(
            client_id=client_id,
            command_type="browse_filesystem",
            payload={
                "request_id": request_id,
                "path": path,
            },
        )

        # Store pending browse request
        storage.save_browse_request(request_id, client_id, path)

        logger.info(f"[BROWSE] Queued browse for agent '{client_id}', path='{path}', id={request_id}")

        return {
            "request_id": request_id,
            "status": "pending",
            "client_id": client_id,
            "path": path,
        }

    @app.get("/api/v1/browse/{request_id}")
    def get_browse_results(
        request_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Get results of a filesystem browse request."""
        result = storage.get_browse_result(request_id)
        if not result:
            raise HTTPException(status_code=404, detail="Browse request not found")
        return result

    @app.post("/api/v1/browse/{request_id}/results")
    async def report_browse_results(
        request_id: str,
        request: Request,
        client: Client = Depends(verify_client),
        storage: Storage = Depends(get_storage),
    ) -> dict[str, str]:
        """Agent reports filesystem browse results."""
        data = await request.json()

        storage.save_browse_result(
            request_id=request_id,
            status="completed" if data.get("success", True) else "error",
            entries=data.get("entries", []),
            error=data.get("error"),
            path=data.get("path", ""),
        )

        logger.info(f"[BROWSE] Results received for request_id={request_id}, entries={len(data.get('entries', []))}")

        return {"status": "recorded"}

    # ============ Restore Operations ============

    @app.post("/api/v1/restore")
    async def trigger_restore(
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Trigger a restore operation.

        Required fields in request body:
        - job_name: Name of the job to restore from
        - client_id: Agent to run the restore on

        Optional fields:
        - destination_path: Where to restore files to (defaults to original source path)
        - run_id: Specific backup run to restore from (defaults to latest)
        - source_subfolder: Subfolder within backup to restore
        - snapshot: For restic, specific snapshot ID to restore (defaults to latest)
        - dry_run: If true, don't actually restore
        """
        data = await request.json()

        job_name = data.get("job_name")
        client_id = data.get("client_id")

        if not job_name:
            raise HTTPException(status_code=400, detail="job_name required")
        if not client_id:
            raise HTTPException(status_code=400, detail="client_id required")

        # Get job config
        job = storage.get_job(job_name)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        # Default destination to original source path if not specified
        destination_path = data.get("destination_path") or job.get("source_path")

        # Verify client exists
        client = storage.get_client(client_id)
        if not client:
            raise HTTPException(status_code=400, detail=f"Agent '{client_id}' not found")

        # Get backup source path (this is the backup destination in the job)
        backup_source = job.get("destination_path")

        # For imported jobs, destination_path might be missing - look up from repository
        if not backup_source and job.get("repository_id"):
            repo = storage.get_repository(job["repository_id"])
            if repo:
                repo_type = repo.get("repo_type", "smb")
                if repo_type == "smb":
                    backup_source = f"//{repo['server']}/{repo['share']}"
                elif repo_type == "nfs":
                    backup_source = f"{repo['server']}:{repo['share']}"
                else:
                    backup_source = repo.get("share", "") or repo.get("path", "")
                if backup_source and repo.get("path"):
                    backup_source = f"{backup_source}/{repo['path']}"

        if not backup_source:
            raise HTTPException(
                status_code=400,
                detail="Job is missing destination_path and no repository configured. "
                       "Please re-import or reconfigure the job."
            )
        backend = job.get("backend", "rclone")
        source_subfolder = data.get("source_subfolder", "")

        # For rclone, append subfolder to source path
        # For restic, subfolder is handled separately via --include flag
        if source_subfolder and backend != "restic":
            backup_source = f"{backup_source}/{source_subfolder}"

        restore_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        started_at = datetime.now()

        # Save restore record
        storage.save_job_run(
            run_id=f"restore_{restore_id}",
            job_name=f"restore:{job_name}",
            status="pending",
            started_at=started_at,
            client_id=client_id,
        )

        # Start progress tracking
        storage.start_job_progress(
            run_id=f"restore_{restore_id}",
            job_name=f"restore:{job_name}",
            client_id=client_id,
        )

        # Queue restore command for the agent
        # Include SMB/NFS credentials for agents that need to mount the backup source
        backend_options = job.get("backend_options", {}).copy()

        command_payload = {
            "job_name": job_name,
            "run_id": f"restore_{restore_id}",
            "source_path": backup_source,  # Restore FROM backup location (repository)
            "destination_path": destination_path,  # Restore TO this location
            "original_source_path": job.get("source_path"),  # For kopia/restic snapshot lookup
            "backend": backend,
            "backend_options": backend_options,
            "snapshot": data.get("snapshot"),
            "source_subfolder": source_subfolder if backend == "restic" else "",  # For restic --include
            "clean_restore": data.get("clean_restore", False),  # Delete extra files at destination
            "dry_run": data.get("dry_run", False),
        }

        # Add repository credentials for SMB/NFS access
        repository_id = job.get("repository_id")
        if repository_id:
            repo = storage.get_repository(repository_id)
            if repo:
                repo_type = repo.get("repo_type", "")
                repo_password = storage.get_repository_password(repository_id)

                if repo_type == "smb":
                    # Include SMB connection info for agents
                    command_payload["smb_server"] = repo.get("server")
                    command_payload["smb_share"] = repo.get("share")
                    command_payload["smb_username"] = repo.get("username")
                    command_payload["smb_domain"] = repo.get("domain")
                    if repo_password:
                        command_payload["smb_password"] = repo_password

                elif repo_type == "nfs":
                    # NFS info (no password needed typically)
                    command_payload["nfs_server"] = repo.get("server")
                    command_payload["nfs_export"] = repo.get("share")

                # For restic/kopia backends, include the repository password
                if backend in ("restic", "kopia") and repo_password:
                    if "password" not in backend_options:
                        command_payload["backend_options"]["password"] = repo_password

        storage.queue_command(
            client_id=client_id,
            command_type="restore",
            payload=command_payload,
        )

        return {
            "restore_id": f"restore_{restore_id}",
            "job_name": job_name,
            "status": "pending",
            "started_at": started_at.isoformat(),
            "message": f"Restore queued for agent '{client_id}'",
        }

    @app.get("/api/v1/jobs/{job_name}/backups")
    def list_job_backups(
        job_name: str,
        limit: int = 20,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List available backups for a job (successful runs that can be restored from)."""
        if not storage.get_job(job_name):
            raise HTTPException(status_code=404, detail="Job not found")

        runs = storage.get_job_runs(job_name, limit=limit)
        # Filter to only successful backups (not restores)
        backups = [
            {
                "run_id": r["run_id"],
                "started_at": r.get("started_at"),
                "finished_at": r.get("finished_at"),
                "bytes_transferred": r.get("bytes_transferred", 0),
                "files_transferred": r.get("files_transferred", 0),
                "snapshot_id": r.get("snapshot_id"),  # For restic backups
            }
            for r in runs
            if r.get("status") == "success" and not r.get("run_id", "").startswith("restore_")
        ]
        return backups

    # ============ Scheduler Status ============

    @app.get("/api/v1/scheduler/status")
    def get_scheduler_status() -> dict[str, Any]:
        """Get scheduler status and upcoming jobs."""
        if not _scheduler:
            return {"enabled": False, "message": "Scheduler not initialized"}

        return {
            "enabled": True,
            "jobs": _scheduler.get_job_status(),
        }

    # ============ Retention Policies ============

    @app.get("/api/v1/retention/presets")
    def get_retention_presets() -> dict[str, Any]:
        """Get available retention policy presets."""
        from backer.server.retention import RETENTION_PRESETS
        return {"presets": RETENTION_PRESETS}

    @app.post("/api/v1/jobs/{job_name}/retention")
    async def set_job_retention(
        job_name: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Set retention policy for a job."""
        job = storage.get_job(job_name)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        data = await request.json()
        job["retention"] = data
        storage.save_job(job_name, job)

        return {"status": "updated", "retention": data}

    @app.post("/api/v1/jobs/{job_name}/retention/apply")
    def apply_job_retention(
        job_name: str,
        dry_run: bool = False,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Apply retention policy to a job (clean up old runs)."""
        from backer.server.retention import RetentionManager

        manager = RetentionManager(storage)
        deleted = manager.apply_retention(job_name, dry_run=dry_run)

        return {
            "job_name": job_name,
            "dry_run": dry_run,
            "deleted_count": len(deleted),
            "deleted_runs": [{"run_id": r["run_id"], "started_at": r.get("started_at")} for r in deleted],
        }

    @app.post("/api/v1/retention/apply-all")
    def apply_all_retention(
        dry_run: bool = False,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Apply retention policies to all jobs."""
        from backer.server.retention import RetentionManager

        manager = RetentionManager(storage)
        results = manager.apply_all_retention(dry_run=dry_run)

        return {
            "dry_run": dry_run,
            "jobs_processed": len(results),
            "total_deleted": sum(len(runs) for runs in results.values()),
            "details": {
                job: [{"run_id": r["run_id"], "started_at": r.get("started_at")} for r in runs]
                for job, runs in results.items()
            },
        }

    # ============ Storage Repositories ============

    @app.post("/api/v1/repositories/discover")
    async def discover_shares_endpoint(
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Discover available shares on a server."""
        from backer.server.repositories import (
            RepositoryType,
        )
        from backer.server.repositories import (
            discover_shares as do_discover,
        )

        data = await request.json()

        repo_type = data.get("type", "smb")
        server = data.get("server", "")
        username = data.get("username")
        password = data.get("password")
        domain = data.get("domain")

        if not server:
            raise HTTPException(status_code=400, detail="Server address required")

        try:
            rtype = RepositoryType(repo_type)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid type: {repo_type}")

        success, result = do_discover(rtype, server, username, password, domain)

        if success:
            return {
                "success": True,
                "shares": [{"name": s.name, "type": s.share_type, "comment": s.comment} for s in result],
            }
        else:
            return {"success": False, "error": result}

    @app.post("/api/v1/repositories/browse")
    async def browse_share_endpoint(
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Browse a directory on a share."""
        from backer.server.repositories import (
            RepositoryType,
            browse_directory,
        )

        data = await request.json()

        repo_type = data.get("type", "smb")
        server = data.get("server", "")
        share = data.get("share", "")
        path = data.get("path", "")
        username = data.get("username")
        password = data.get("password")
        domain = data.get("domain")

        if not server or not share:
            raise HTTPException(status_code=400, detail="Server and share required")

        try:
            rtype = RepositoryType(repo_type)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid type: {repo_type}")

        success, result = browse_directory(rtype, server, share, path, username, password, domain)

        if success:
            return {
                "success": True,
                "path": path,
                "entries": [
                    {"name": e.name, "is_dir": e.is_dir, "size": e.size}
                    for e in result
                ],
            }
        else:
            return {"success": False, "error": result}

    @app.get("/api/v1/repositories")
    def list_repositories(storage: Storage = Depends(get_storage)) -> list[dict[str, Any]]:
        """List all storage repositories."""
        return storage.list_repositories()

    @app.post("/api/v1/repositories")
    async def create_repository(
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Create a new storage repository."""
        from backer.server.secrets import get_secrets_manager

        data = await request.json()

        name = data.get("name", "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Name required")
        # Validate repository name for security
        validate_name(name, "Repository name")

        if storage.get_repository_by_name(name):
            raise HTTPException(status_code=409, detail="Repository name already exists")

        repo_id = str(uuid4())[:8]
        password = data.get("password")
        password_encrypted = None
        if password:
            secrets = get_secrets_manager(storage.db_path.parent)
            password_encrypted = secrets.encrypt(password)

        storage.add_repository(
            repo_id=repo_id,
            name=name,
            repo_type=data.get("type", "smb"),
            server=data.get("server"),
            share=data.get("share"),
            path=data.get("path", ""),
            username=data.get("username"),
            password_encrypted=password_encrypted,
            domain=data.get("domain"),
        )

        return {"id": repo_id, "name": name, "status": "created"}

    @app.delete("/api/v1/repositories/{repo_id}")
    def delete_repository(repo_id: str, storage: Storage = Depends(get_storage)) -> dict[str, str]:
        """Delete a repository."""
        if not storage.delete_repository(repo_id):
            raise HTTPException(status_code=404, detail="Repository not found")
        return {"status": "deleted"}

    @app.post("/api/v1/repositories/{repo_id}/test")
    def test_repository(repo_id: str, storage: Storage = Depends(get_storage)) -> dict[str, Any]:
        """Test connection to a repository."""
        from backer.server.repositories import NFSBrowser, SMBBrowser

        repo = storage.get_repository(repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found")

        password = storage.get_repository_password(repo_id)

        if repo["repo_type"] == "smb":
            success, message = SMBBrowser.test_connection(
                server=repo["server"],
                share=repo["share"],
                username=repo["username"],
                password=password,
                domain=repo["domain"],
            )
        elif repo["repo_type"] == "nfs":
            # For NFS, just try to list the export
            success, result = NFSBrowser.list_exports(repo["server"])
            if success:
                message = f"NFS server responding, {len(result)} exports available"
            else:
                message = result
        else:
            success, message = False, "Test not supported for this repository type"

        # Update status
        storage.update_repository_status(repo_id, "connected" if success else "error")

        return {"success": success, "message": message}

    @app.post("/api/v1/repositories/{repo_id}/scan")
    def scan_repository(repo_id: str, storage: Storage = Depends(get_storage)) -> dict[str, Any]:
        """Scan a repository for existing Backer metadata and backups.

        This discovers what backups exist in the repository, which can then
        be imported into the server database.

        For SMB shares, uses smbclient to read files directly (no root needed).
        For local/mounted paths, accesses the filesystem directly.
        """
        import json as json_module

        from backer.server.repositories import smb_list_files, smb_read_file

        repo = storage.get_repository(repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found")

        repo_type = repo.get("repo_type", "smb")
        subpath = repo.get("path", "")
        server = repo.get("server", "")
        share = repo.get("share", "")
        username = repo.get("username")
        password = storage.get_repository_password(repo_id)
        domain = repo.get("domain")

        # Build display path
        if repo_type == "smb":
            display_path = f"//{server}/{share}"
        elif repo_type == "nfs":
            display_path = f"{server}:{share}"
        else:
            display_path = share
        if subpath:
            display_path = f"{display_path}/{subpath}"

        def format_result(discovery: dict) -> dict[str, Any]:
            """Format discovery result for API response."""
            return {
                "success": True,
                "repository_id": repo_id,
                "repository_name": repo.get("name"),
                "path": display_path,
                "initialized": discovery.get("initialized", False),
                "summary": discovery.get("summary", {}),
                "agents": discovery.get("agents", []),
                "jobs": [
                    {
                        "job_name": j.get("job_name"),
                        "run_count": j.get("run_count", 0),
                        "created_at": j.get("created_at"),
                        "updated_at": j.get("updated_at"),
                    }
                    for j in discovery.get("jobs", [])
                ],
                "snapshots": [
                    {
                        "snapshot_id": s.get("snapshot_id"),
                        "short_id": s.get("short_id"),
                        "hostname": s.get("hostname"),
                        "time": s.get("time"),
                        "paths": s.get("paths", []),
                    }
                    for s in discovery.get("snapshots", [])[:50]
                ],
            }

        try:
            # Use direct filesystem access for local or mounted paths
            if repo.get("mount_point") or repo_type == "local":
                from backer.core.repo_metadata import RepositoryMetadata

                if repo.get("mount_point"):
                    repo_path = repo["mount_point"]
                    if subpath:
                        repo_path = repo_path.rstrip("/") + "/" + subpath
                else:
                    repo_path = share or repo.get("path", "")

                repo_meta = RepositoryMetadata(repo_path, repo_type)
                return format_result(repo_meta.discover_all())

            # For SMB shares, use smbclient to read metadata directly (no mount needed)
            elif repo_type == "smb":
                metadata_base = f"{subpath}/.backer" if subpath else ".backer"

                # Try to read metadata.json
                success, content = smb_read_file(
                    server, share, f"{metadata_base}/metadata.json",
                    username, password, domain
                )

                if not success:
                    # No metadata found
                    return format_result({
                        "initialized": False,
                        "agents": [],
                        "jobs": [],
                        "snapshots": [],
                        "summary": {"agent_count": 0, "job_count": 0, "snapshot_count": 0, "total_runs": 0},
                    })

                # Parse metadata
                try:
                    metadata = json_module.loads(content)
                except json_module.JSONDecodeError:
                    metadata = {}

                # Read agents
                agents = []
                ok, agent_files = smb_list_files(
                    server, share, f"{metadata_base}/agents", username, password, domain
                )
                if ok:
                    for f in agent_files:
                        if f.endswith(".json"):
                            ok2, c = smb_read_file(
                                server, share, f"{metadata_base}/agents/{f}",
                                username, password, domain
                            )
                            if ok2:
                                try:
                                    agents.append(json_module.loads(c))
                                except json_module.JSONDecodeError:
                                    pass

                # Read jobs
                jobs = []
                ok, job_dirs = smb_list_files(
                    server, share, f"{metadata_base}/jobs", username, password, domain
                )
                if ok:
                    for d in job_dirs:
                        ok2, c = smb_read_file(
                            server, share, f"{metadata_base}/jobs/{d}/config.json",
                            username, password, domain
                        )
                        if ok2:
                            try:
                                job = json_module.loads(c)
                                # Count runs
                                ok3, runs = smb_list_files(
                                    server, share, f"{metadata_base}/jobs/{d}/runs",
                                    username, password, domain
                                )
                                run_count = len([r for r in runs if r.endswith(".json")])
                                job["run_count"] = run_count if ok3 else 0
                                jobs.append(job)
                            except json_module.JSONDecodeError:
                                pass

                # Read snapshots
                snapshots = []
                ok, snap_files = smb_list_files(
                    server, share, f"{metadata_base}/snapshots", username, password, domain
                )
                if ok:
                    for f in snap_files:
                        if f.endswith(".json"):
                            ok2, c = smb_read_file(
                                server, share, f"{metadata_base}/snapshots/{f}",
                                username, password, domain
                            )
                            if ok2:
                                try:
                                    snapshots.append(json_module.loads(c))
                                except json_module.JSONDecodeError:
                                    pass

                return format_result({
                    "initialized": True,
                    "metadata": metadata,
                    "agents": agents,
                    "jobs": jobs,
                    "snapshots": snapshots,
                    "summary": {
                        "agent_count": len(agents),
                        "job_count": len(jobs),
                        "snapshot_count": len(snapshots),
                        "total_runs": sum(j.get("run_count", 0) for j in jobs),
                    },
                })

            else:
                # NFS without mount_point - not supported
                return {
                    "success": False,
                    "error": "NFS scanning requires mount_point to be set",
                    "repository_id": repo_id,
                    "repository_name": repo.get("name"),
                    "path": display_path,
                    "hint": "Mount the NFS share and set the mount_point field",
                }

        except Exception as e:
            logger.error(f"Failed to scan repository {repo_id}: {e}")
            return {
                "success": False,
                "error": str(e),
                "repository_id": repo_id,
            }

    @app.post("/api/v1/repositories/{repo_id}/scan-restic")
    async def scan_restic_repository(
        repo_id: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Scan a restic repository for snapshots and sync metadata.

        This queries restic directly to find snapshots and updates the
        repository metadata. Requires the restic password.
        """
        from backer.server.repo_metadata import (
            RepositoryMetadata,
            ResticRepositoryScanner,
        )

        repo = storage.get_repository(repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found")

        data = await request.json()
        password = data.get("password")
        if not password:
            # Try to get password from repository or job configuration
            password = storage.get_repository_password(repo_id)
            if not password:
                raise HTTPException(
                    status_code=400,
                    detail="Restic password required (provide in request body or repository config)"
                )

        # Build repository path
        repo_type = repo.get("repo_type", "smb")
        if repo_type == "smb":
            repo_path = f"//{repo['server']}/{repo['share']}"
            if repo.get("path"):
                repo_path += "/" + repo["path"]
        elif repo_type == "nfs":
            repo_path = f"{repo['server']}:{repo['share']}"
            if repo.get("path"):
                repo_path += "/" + repo["path"]
        elif repo_type == "local":
            repo_path = repo.get("share", "") or repo.get("path", "")
        else:
            repo_path = repo.get("share", "") or repo.get("path", "")

        try:
            # First, ensure metadata is initialized
            repo_meta = RepositoryMetadata(repo_path, repo_type)
            if not repo_meta.is_initialized():
                repo_meta.initialize()

            # Scan restic repo and sync to metadata
            scanner = ResticRepositoryScanner(repo_path, password)

            if not scanner.is_restic_repo():
                return {
                    "success": False,
                    "error": "Not a valid restic repository or incorrect password",
                    "repository_id": repo_id,
                }

            sync_result = scanner.scan_and_sync(repo_meta)

            return {
                "success": True,
                "repository_id": repo_id,
                "repository_name": repo.get("name"),
                "path": repo_path,
                **sync_result,
            }
        except Exception as e:
            logger.error(f"Failed to scan restic repository {repo_id}: {e}")
            return {
                "success": False,
                "error": str(e),
                "repository_id": repo_id,
            }

    @app.post("/api/v1/repositories/{repo_id}/import")
    def import_repository_metadata_endpoint(
        repo_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Import discovered metadata from repository into server database.

        This imports jobs, runs, and agent references from the repository
        metadata into the server's database, enabling management of
        previously-created backups.

        For SMB shares, uses smbclient to read files directly (no root needed).
        For local/mounted paths, accesses the filesystem directly.
        """
        import json as json_module
        from datetime import datetime as dt

        from backer.server.repositories import smb_list_files, smb_read_file

        repo = storage.get_repository(repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found")

        repo_type = repo.get("repo_type", "smb")
        subpath = repo.get("path", "")
        server = repo.get("server", "")
        share = repo.get("share", "")
        username = repo.get("username")
        password = storage.get_repository_password(repo_id)
        domain = repo.get("domain")

        try:
            # Use direct filesystem access for local or mounted paths
            if repo.get("mount_point") or repo_type == "local":
                from backer.server.repo_metadata import import_repository_metadata

                if repo.get("mount_point"):
                    repo_path = repo["mount_point"]
                    if subpath:
                        repo_path = repo_path.rstrip("/") + "/" + subpath
                else:
                    repo_path = share or repo.get("path", "")

                result = import_repository_metadata(repo_path, storage, repo_id)
                return {
                    "repository_id": repo_id,
                    "repository_name": repo.get("name"),
                    **result,
                }

            # For SMB shares, use smbclient to read metadata directly
            elif repo_type == "smb":
                metadata_base = f"{subpath}/.backer" if subpath else ".backer"
                imported = {"agents": 0, "jobs": 0, "runs": 0}

                # Check if repository has backer metadata
                success, content = smb_read_file(
                    server, share, f"{metadata_base}/metadata.json",
                    username, password, domain
                )

                if not success:
                    return {"error": "Repository has no Backer metadata"}

                # Read and import jobs
                # Build the repository path for imported jobs
                repo_path = f"//{server}/{share}"
                if subpath:
                    repo_path = f"{repo_path}/{subpath}"

                ok, job_dirs = smb_list_files(
                    server, share, f"{metadata_base}/jobs", username, password, domain
                )
                if ok:
                    for job_dir in job_dirs:
                        ok2, job_content = smb_read_file(
                            server, share, f"{metadata_base}/jobs/{job_dir}/config.json",
                            username, password, domain
                        )
                        if ok2:
                            try:
                                job_data = json_module.loads(job_content)
                                job_name = job_data.get("job_name")
                                config = job_data.get("config", {})

                                if job_name:
                                    existing = storage.get_job(job_name)
                                    if not existing:
                                        config["repository_id"] = repo_id
                                        config["imported_at"] = dt.now().isoformat()
                                        config["imported_from_repo"] = True
                                        # Set destination_path to repo path for restores
                                        if not config.get("destination_path"):
                                            config["destination_path"] = repo_path
                                        # Set backend if not present
                                        if not config.get("backend"):
                                            config["backend"] = "kopia"
                                        storage.save_job(job_name, config)
                                        imported["jobs"] += 1

                                        # Import job runs
                                        ok3, run_files = smb_list_files(
                                            server, share,
                                            f"{metadata_base}/jobs/{job_dir}/runs",
                                            username, password, domain
                                        )
                                        if ok3:
                                            for run_file in run_files:
                                                if run_file.endswith(".json"):
                                                    ok4, run_content = smb_read_file(
                                                        server, share,
                                                        f"{metadata_base}/jobs/{job_dir}/runs/{run_file}",
                                                        username, password, domain
                                                    )
                                                    if ok4:
                                                        try:
                                                            run = json_module.loads(run_content)
                                                            run_id = run.get("run_id")
                                                            if run_id:
                                                                started_at = dt.now()
                                                                finished_at = None
                                                                if run.get("started_at"):
                                                                    try:
                                                                        started_at = dt.fromisoformat(
                                                                            run["started_at"]
                                                                        )
                                                                    except (ValueError, TypeError):
                                                                        pass
                                                                if run.get("finished_at"):
                                                                    try:
                                                                        finished_at = dt.fromisoformat(
                                                                            run["finished_at"]
                                                                        )
                                                                    except (ValueError, TypeError):
                                                                        pass
                                                                storage.save_job_run(
                                                                    run_id=run_id,
                                                                    job_name=job_name,
                                                                    status=run.get("status", "unknown"),
                                                                    started_at=started_at,
                                                                    finished_at=finished_at,
                                                                    bytes_transferred=run.get(
                                                                        "bytes_transferred", 0
                                                                    ),
                                                                    files_transferred=run.get(
                                                                        "files_transferred", 0
                                                                    ),
                                                                    snapshot_id=run.get("snapshot_id"),
                                                                )
                                                                imported["runs"] += 1
                                                        except json_module.JSONDecodeError:
                                                            pass
                            except json_module.JSONDecodeError:
                                pass

                # Read agents (just count them - we can't fully import without secrets)
                ok, agent_files = smb_list_files(
                    server, share, f"{metadata_base}/agents", username, password, domain
                )
                if ok:
                    imported["agents"] = len([f for f in agent_files if f.endswith(".json")])
                    logger.info(f"Discovered {imported['agents']} agents in repository metadata")

                return {
                    "success": True,
                    "repository_id": repo_id,
                    "repository_name": repo.get("name"),
                    "imported": imported,
                }

            else:
                # NFS without mount_point - not supported
                return {
                    "success": False,
                    "error": "NFS import requires mount_point to be set",
                    "repository_id": repo_id,
                    "repository_name": repo.get("name"),
                    "hint": "Mount the NFS share and set the mount_point field",
                }

        except Exception as e:
            logger.error(f"Failed to import metadata from repository {repo_id}: {e}")
            return {
                "success": False,
                "error": str(e),
                "repository_id": repo_id,
            }

    # ============ Web UI ============

    # Store storage in app state for web routes
    app.state.storage = _storage

    # Mount static files
    static_dir = Path(__file__).parent / "web" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Include web UI routes
    app.include_router(web_router)

    return app
