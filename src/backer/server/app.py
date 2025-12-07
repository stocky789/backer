"""FastAPI application for the Backer server."""

import hashlib
import json
import logging
import re
import secrets
import time
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


def _get_job_subfolder(job_name: str) -> str:
    """Get a safe subfolder name for a job.

    Each job gets its own subfolder within the repository to prevent conflicts
    between different backup jobs (especially important for restic/kopia which
    initialize the repository on first use).
    """
    # Sanitize job name for use as folder name
    # Replace any unsafe characters with underscores
    import re
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', job_name)
    return safe_name


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

    Each job automatically gets its own subfolder within the destination
    to prevent conflicts between different backup jobs.
    """
    storage = storage or _storage
    if storage is None:
        raise RuntimeError("Storage not initialized")

    # Start with basic payload
    backend_options = job.get("backend_options", {}).copy()
    backend = job.get("backend", "rclone")

    # Get base destination and append job-specific subfolder under Agents/
    # Final structure: {repo_path}/Agents/{job_name}
    base_destination = job.get("destination_path", "")
    job_subfolder = _get_job_subfolder(job_name)
    if base_destination:
        # Normalize path separators and append Agents/ prefix with subfolder
        destination_path = f"{base_destination.rstrip('/')}/Agents/{job_subfolder}"
    else:
        destination_path = ""

    payload = {
        "job_name": job_name,
        "run_id": run_id,
        "source_path": job.get("source_path"),
        "destination_path": destination_path,
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


def trigger_hypervisor_job_internal(job_id: str) -> None:
    """Internal function to trigger a hypervisor backup job (used by scheduler).

    This runs the backup synchronously in a background thread.
    Backups are stored to the configured Backer repository by auto-configuring
    it as Proxmox storage (like Veeam does).
    """
    from backer.hypervisors.proxmox import (
        ProxmoxAPI,
        ProxmoxAPIError,
        ProxmoxAuthMethod,
        ProxmoxBackupManager,
        ProxmoxBackupMode,
        ProxmoxCompression,
    )

    if _storage is None:
        logger.error("Storage not initialized")
        return

    job = _storage.get_hypervisor_job(job_id)
    if not job:
        logger.error(f"Hypervisor job not found: {job_id}")
        return

    hypervisor = _storage.get_hypervisor(job["hypervisor_id"])
    if not hypervisor:
        logger.error(f"Hypervisor not found for job: {job_id}")
        return

    if hypervisor["hypervisor_type"] != "proxmox":
        logger.error(f"Unsupported hypervisor type: {hypervisor['hypervisor_type']}")
        return

    # Get repository for backup destination
    repository_id = job.get("repository_id")
    if not repository_id:
        logger.error(f"No repository configured for job: {job_id}")
        return

    repository = _storage.get_repository(repository_id)
    if not repository:
        logger.error(f"Repository not found: {repository_id}")
        return

    # Validate repository type (must be SMB or NFS for Proxmox storage)
    repo_type = repository.get("repo_type", "").lower()
    if repo_type not in ("smb", "nfs"):
        logger.error(
            f"Repository type '{repo_type}' is not supported for hypervisor backups. "
            "Use an SMB or NFS repository so Proxmox can write directly to it."
        )
        return

    # Get credentials
    token_secret = _storage.get_hypervisor_token_secret(hypervisor["id"])
    hv_password = _storage.get_hypervisor_password(hypervisor["id"])

    auth_method = ProxmoxAuthMethod.TOKEN if hypervisor["auth_method"] == "token" else ProxmoxAuthMethod.PASSWORD

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    proxmox_storage_id = None  # Track for cleanup

    try:
        api = ProxmoxAPI(
            host=hypervisor["host"],
            port=hypervisor.get("port", 8006),
            auth_method=auth_method,
            username=hypervisor.get("username"),
            token_id=hypervisor.get("token_id"),
            token_secret=token_secret,
            password=hv_password,
            verify_ssl=hypervisor.get("verify_ssl", False),
        )

        # Authenticate if using password-based auth
        if auth_method == ProxmoxAuthMethod.PASSWORD:
            api.authenticate()

        # Get repository password for SMB storage
        repo_password = None
        if repo_type == "smb" and repository.get("has_password"):
            repo_password = _storage.get_repository_password(repository_id)

        # Create repository dict with password for ensure_backer_storage
        repo_with_password = {**repository, "password": repo_password}

        # Ensure Proxmox storage exists for this repository
        # Backups go to: {repo_path}/Hypervisors/{hypervisor_name}/dump/
        try:
            proxmox_storage_id = api.ensure_backer_storage(
                repo_with_password,
                hypervisor_name=hypervisor["name"],
            )
        except ProxmoxAPIError as e:
            logger.error(f"Failed to configure Proxmox storage for repository: {e}")
            return

        backup_manager = ProxmoxBackupManager(api)

        # Get backup options
        mode_map = {
            "snapshot": ProxmoxBackupMode.SNAPSHOT,
            "stop": ProxmoxBackupMode.STOP,
            "suspend": ProxmoxBackupMode.SUSPEND,
        }
        mode = mode_map.get(job.get("backup_mode", "snapshot"), ProxmoxBackupMode.SNAPSHOT)

        compress_map = {
            "zstd": ProxmoxCompression.ZSTD,
            "gzip": ProxmoxCompression.GZIP,
            "lzo": ProxmoxCompression.LZO,
            "none": ProxmoxCompression.NONE,
        }
        compress = compress_map.get(job.get("compression", "zstd"), ProxmoxCompression.ZSTD)

        # Get guest IDs from job
        guest_ids = job.get("guest_ids", [])

        if not guest_ids:
            # Backup all guests
            guests = api.list_guests()
            guest_ids = [g.vmid for g in guests]

        # Build guest name map
        all_guests = api.list_guests()
        guest_map = {g.vmid: g for g in all_guests}

        logger.info(
            f"Starting hypervisor backup job '{job['name']}' to repository "
            f"'{repository['name']}' (Proxmox storage: {proxmox_storage_id})"
        )

        for vmid in guest_ids:
            guest = guest_map.get(vmid)
            guest_name = guest.name if guest else f"VM {vmid}"

            # Save run as running
            _storage.save_hypervisor_run(
                run_id=run_id,
                job_id=job_id,
                job_name=job["name"],
                hypervisor_id=hypervisor["id"],
                guest_id=vmid,
                guest_name=guest_name,
                status="running",
            )

            try:
                # Backup directly to Proxmox storage (which points to Backer repo)
                result = backup_manager.backup_to_storage(
                    vmid=vmid,
                    storage=proxmox_storage_id,
                    mode=mode,
                    compress=compress,
                    timeout=7200,
                )

                # Update run record
                _storage.save_hypervisor_run(
                    run_id=run_id,
                    job_id=job_id,
                    job_name=job["name"],
                    hypervisor_id=hypervisor["id"],
                    guest_id=vmid,
                    guest_name=guest_name,
                    status="success" if result.get("success") else "failed",
                    upid=result.get("upid"),
                    finished_at=(
                        datetime.fromisoformat(result["finished_at"])
                        if result.get("finished_at") else datetime.now()
                    ),
                    duration_seconds=result.get("duration_seconds"),
                    exit_status=result.get("exit_status"),
                    errors=result.get("errors"),
                )

                if result.get("success"):
                    backup_size = result.get("backup_size", 0)
                    logger.info(
                        f"Backup succeeded for VMID {vmid} -> {proxmox_storage_id} "
                        f"({backup_size / 1024 / 1024:.1f} MB)"
                    )
                else:
                    logger.warning(f"Backup failed for VMID {vmid}: {result.get('errors')}")

            except Exception as e:
                logger.exception(f"Backup failed for VMID {vmid}: {e}")
                _storage.save_hypervisor_run(
                    run_id=run_id,
                    job_id=job_id,
                    job_name=job["name"],
                    hypervisor_id=hypervisor["id"],
                    guest_id=vmid,
                    guest_name=guest_name,
                    status="failed",
                    finished_at=datetime.now(),
                    errors=[str(e)],
                )

        # Clean up: remove the temporary Proxmox storage
        # This unmounts the share, keeping Proxmox UI clean
        try:
            api.delete_storage(proxmox_storage_id)
            logger.info(f"Removed temporary Proxmox storage '{proxmox_storage_id}'")
        except Exception as cleanup_err:
            # Log but don't fail the backup - cleanup is best-effort
            logger.warning(f"Failed to cleanup Proxmox storage '{proxmox_storage_id}': {cleanup_err}")

        logger.info(f"Scheduled hypervisor job '{job.get('name')}' completed")

    except Exception as e:
        logger.exception(f"Hypervisor job {job_id} failed: {e}")
        # Try to cleanup storage even on failure
        if proxmox_storage_id:
            try:
                api.delete_storage(proxmox_storage_id)
            except Exception:
                pass  # Best effort cleanup


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
        hypervisor_job_trigger_callback=trigger_hypervisor_job_internal,
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

        client_name = client.name or client_id

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

        The job is removed from the database immediately.
        Metadata cleanup from repositories runs in the background.

        Args:
            job_name: Name of the job to delete
            delete_snapshots: If True, also delete associated snapshot metadata
        """
        # Get job config before deleting (to find repository)
        job = storage.get_job(job_name)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        logger.info(f"[DELETE JOB] Deleting job '{job_name}', delete_snapshots={delete_snapshots}")

        # Capture info needed for background cleanup
        repo_id = job.get("repository_id")
        repo_info = None

        if repo_id:
            repo = storage.get_repository(repo_id)
            if repo:
                repo_info = {
                    "repo_type": repo.get("repo_type", "smb"),
                    "server": repo.get("server", ""),
                    "share": repo.get("share", ""),
                    "path": repo.get("path", ""),
                    "username": repo.get("username"),
                    "password": storage.get_repository_password(repo_id),
                    "domain": repo.get("domain"),
                    "mount_point": repo.get("mount_point"),
                }

        # Delete job from database immediately
        if not storage.delete_job(job_name):
            raise HTTPException(status_code=404, detail="Job not found")

        result: dict[str, Any] = {"status": "deleted"}

        # Schedule metadata cleanup as a background task if we have a repository
        if repo_info:
            # Get the job subfolder name for potential full deletion
            job_subfolder = _get_job_subfolder(job_name)

            def cleanup_job_metadata(task: Task) -> dict[str, Any]:
                """Background task to clean up job metadata from repository.

                If delete_snapshots is True, deletes the entire job subfolder
                (including all backup data). Otherwise, only deletes metadata.
                """
                import shutil
                from pathlib import Path as PathLib

                from backer.server.repositories import (
                    smb_delete_directory,
                )

                job_deleted = False
                subfolder_deleted = False
                errors = []

                repo_type = repo_info["repo_type"]
                server = repo_info["server"]
                share = repo_info["share"]
                subpath = repo_info["path"]
                username = repo_info["username"]
                password = repo_info["password"]
                domain = repo_info["domain"]

                # Job subfolder path (where all backup data lives)
                job_subfolder_path = f"{subpath}/{job_subfolder}" if subpath else job_subfolder

                # Metadata path within the job subfolder
                metadata_job_path = f"{job_subfolder_path}/.backer/jobs/{job_name}"

                task.message = "Deleting job data..." if delete_snapshots else "Deleting job metadata..."
                task.progress = 10

                try:
                    if repo_type == "smb":
                        if delete_snapshots:
                            # Delete the entire job subfolder (all backup data + metadata)
                            task.message = f"Deleting job subfolder '{job_subfolder}'..."
                            success, msg = smb_delete_directory(
                                server, share, job_subfolder_path,
                                username, password, domain
                            )
                            if success:
                                subfolder_deleted = True
                                job_deleted = True
                                logger.info(f"Deleted entire job subfolder from SMB: {job_subfolder_path}")
                            elif "no such file" not in msg.lower() and "not found" not in msg.lower():
                                errors.append(f"Job subfolder: {msg}")
                        else:
                            # Only delete job metadata, keep backup data
                            success, msg = smb_delete_directory(
                                server, share, metadata_job_path,
                                username, password, domain
                            )
                            if success:
                                job_deleted = True
                                logger.info(f"Deleted job metadata from SMB: {metadata_job_path}")
                            elif "no such file" not in msg.lower() and "not found" not in msg.lower():
                                errors.append(f"Job metadata: {msg}")

                        task.progress = 90

                    elif repo_type == "local" or repo_info.get("mount_point"):
                        # Direct filesystem access
                        if repo_info.get("mount_point"):
                            base_path = PathLib(repo_info["mount_point"])
                        else:
                            base_path = PathLib(share or repo_info.get("path", ""))

                        if subpath:
                            base_path = base_path / subpath

                        if delete_snapshots:
                            # Delete entire job subfolder
                            job_folder_path = base_path / job_subfolder
                            if job_folder_path.exists():
                                shutil.rmtree(job_folder_path)
                                subfolder_deleted = True
                                job_deleted = True
                                logger.info(f"Deleted entire job subfolder: {job_folder_path}")
                        else:
                            # Only delete metadata
                            job_metadata_path = base_path / job_subfolder / ".backer" / "jobs" / job_name
                            if job_metadata_path.exists():
                                shutil.rmtree(job_metadata_path)
                                job_deleted = True

                        task.progress = 90

                    # NFS - similar to local if mounted
                    elif repo_type == "nfs" and repo_info.get("mount_point"):
                        base_path = PathLib(repo_info["mount_point"])
                        if subpath:
                            base_path = base_path / subpath

                        if delete_snapshots:
                            job_folder_path = base_path / job_subfolder
                            if job_folder_path.exists():
                                shutil.rmtree(job_folder_path)
                                subfolder_deleted = True
                                job_deleted = True
                        else:
                            job_metadata_path = base_path / job_subfolder / ".backer" / "jobs" / job_name
                            if job_metadata_path.exists():
                                shutil.rmtree(job_metadata_path)
                                job_deleted = True

                except Exception as e:
                    errors.append(str(e))
                    logger.exception(f"Error in job metadata cleanup: {e}")

                return {
                    "job_deleted": job_deleted,
                    "subfolder_deleted": subfolder_deleted,
                    "errors": errors,
                }

            task_manager = get_task_manager()
            task_desc = (
                f"Deleting all data for job '{job_name}'"
                if delete_snapshots
                else f"Cleaning up metadata for job '{job_name}'"
            )
            task = task_manager.submit(
                task_type="delete_job_data" if delete_snapshots else "delete_job_metadata",
                description=task_desc,
                func=cleanup_job_metadata,
            )
            result["cleanup_task_id"] = task.id
            result["message"] = (
                "Job deleted. Backup data cleanup running in background."
                if delete_snapshots
                else "Job deleted. Metadata cleanup running in background."
            )

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
        # Note: This is the base path from job config, we need to add job subfolder
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

        # Append job-specific subfolder to backup source
        # This matches where _build_backup_command_payload puts the backup
        job_subfolder = _get_job_subfolder(job_name)
        backup_source = f"{backup_source.rstrip('/')}/{job_subfolder}"

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
        import asyncio

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

        # Run blocking SMB operation in thread pool to not block event loop
        loop = asyncio.get_event_loop()
        success, result = await loop.run_in_executor(
            None, lambda: do_discover(rtype, server, username, password, domain)
        )

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
        import asyncio

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

        # Run blocking SMB operation in thread pool to not block event loop
        loop = asyncio.get_event_loop()
        success, result = await loop.run_in_executor(
            None, lambda: browse_directory(rtype, server, share, path, username, password, domain)
        )

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
        """Test connection to a repository.

        This runs as a background task to avoid blocking the UI.
        Returns a task_id that can be polled for results.
        """
        repo = storage.get_repository(repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found")

        repo_name = repo.get("name", repo_id)
        repo_type = repo.get("repo_type", "smb")
        server = repo.get("server", "")
        share = repo.get("share", "")
        username = repo.get("username")
        password = storage.get_repository_password(repo_id)
        domain = repo.get("domain")

        def test_connection(task: Task) -> dict[str, Any]:
            """Background task to test repository connection."""
            from backer.server.repositories import NFSBrowser, SMBBrowser

            task.message = f"Testing connection to {server}..."
            task.progress = 20

            success = False
            message = ""

            try:
                if repo_type == "smb":
                    task.message = f"Connecting to SMB share //{server}/{share}..."
                    task.progress = 40
                    success, message = SMBBrowser.test_connection(
                        server=server,
                        share=share,
                        username=username,
                        password=password,
                        domain=domain,
                    )
                elif repo_type == "nfs":
                    task.message = f"Connecting to NFS server {server}..."
                    task.progress = 40
                    success, result = NFSBrowser.list_exports(server)
                    if success:
                        message = f"NFS server responding, {len(result)} exports available"
                    else:
                        message = result
                else:
                    success, message = False, "Test not supported for this repository type"

                task.progress = 80

                # Update status in database
                storage.update_repository_status(repo_id, "connected" if success else "error")

            except Exception as e:
                success = False
                message = str(e)
                logger.exception(f"Repository test failed: {e}")

            return {"success": success, "message": message, "repo_id": repo_id}

        task_manager = get_task_manager()
        task = task_manager.submit(
            task_type="test_repository",
            description=f"Testing connection to '{repo_name}'",
            func=test_connection,
        )

        return {"task_id": task.id, "status": "testing", "message": "Connection test started"}

    @app.post("/api/v1/repositories/{repo_id}/scan")
    def scan_repository(repo_id: str, storage: Storage = Depends(get_storage)) -> dict[str, Any]:
        """Scan a repository for existing Backer metadata and backups.

        This runs as a background task to avoid blocking the UI.
        Returns a task_id that can be polled for results.
        """
        repo = storage.get_repository(repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found")

        repo_name = repo.get("name", repo_id)
        repo_type = repo.get("repo_type", "smb")
        subpath = repo.get("path", "")
        server = repo.get("server", "")
        share = repo.get("share", "")
        username = repo.get("username")
        password = storage.get_repository_password(repo_id)
        domain = repo.get("domain")
        mount_point = repo.get("mount_point")

        # Build display path
        if repo_type == "smb":
            display_path = f"//{server}/{share}"
        elif repo_type == "nfs":
            display_path = f"{server}:{share}"
        else:
            display_path = share
        if subpath:
            display_path = f"{display_path}/{subpath}"

        def scan_repo_task(task: Task) -> dict[str, Any]:
            """Background task to scan repository."""
            import json as json_module

            from backer.server.repositories import smb_list_files, smb_read_file

            def format_result(discovery: dict) -> dict[str, Any]:
                """Format discovery result for API response."""
                result = {
                    "success": True,
                    "repository_id": repo_id,
                    "repository_name": repo_name,
                    "path": display_path,
                    "initialized": discovery.get("initialized", False),
                    "summary": discovery.get("summary", {}),
                    "agents": discovery.get("agents", []),
                    "hypervisors": discovery.get("hypervisors", []),
                    "guests": discovery.get("guests", []),
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
                # Include scan diagnostic info if present
                if discovery.get("scan_path"):
                    result["scan_path"] = discovery["scan_path"]
                if discovery.get("scan_note"):
                    result["scan_note"] = discovery["scan_note"]
                return result

            try:
                task.message = "Connecting to repository..."
                task.progress = 10

                # Use direct filesystem access for local or mounted paths
                if mount_point or repo_type == "local":
                    from backer.core.repo_metadata import RepositoryMetadata

                    if mount_point:
                        repo_path = mount_point
                        if subpath:
                            repo_path = repo_path.rstrip("/") + "/" + subpath
                    else:
                        repo_path = share or repo.get("path", "")

                    task.message = "Reading local metadata..."
                    task.progress = 30
                    repo_meta = RepositoryMetadata(repo_path, repo_type)
                    return format_result(repo_meta.discover_all())

                # For SMB shares, use smbclient to read metadata directly
                elif repo_type == "smb":
                    # Scan job subfolders for metadata
                    # Each job has its own subfolder: repo_root/job_name/.backer/
                    base_path = subpath if subpath else ""

                    task.message = "Listing job subfolders..."
                    task.progress = 15
                    logger.info(f"[SCAN] Listing job subfolders at //{server}/{share}/{base_path}")

                    # List directories at the repo root (these are job subfolders)
                    ok, entries = smb_list_files(
                        server, share, base_path,
                        username, password, domain
                    )

                    all_agents = []
                    all_jobs = []
                    all_snapshots = []
                    found_any_metadata = False
                    agent_ids_seen = set()

                    if ok:
                        # Filter to get just directory names (job subfolders)
                        job_folders = [e for e in entries if not e.startswith('.') and not e.endswith('.json')]

                        total_folders = len(job_folders)
                        for idx, job_folder in enumerate(job_folders):
                            progress_pct = 20 + int((idx / max(total_folders, 1)) * 60)
                            task.progress = progress_pct
                            task.message = f"Scanning job folder: {job_folder}..."

                            folder_path = f"{base_path}/{job_folder}" if base_path else job_folder
                            metadata_base = f"{folder_path}/.backer"

                            # Check if this folder has .backer metadata
                            ok2, content = smb_read_file(
                                server, share, f"{metadata_base}/metadata.json",
                                username, password, domain
                            )

                            if not ok2:
                                # No metadata in this folder, skip
                                continue

                            found_any_metadata = True
                            logger.info(f"[SCAN] Found metadata in job folder: {job_folder}")

                            # Read agents (dedup by agent_id)
                            ok3, agent_files = smb_list_files(
                                server, share, f"{metadata_base}/agents", username, password, domain
                            )
                            if ok3:
                                for f in agent_files:
                                    if f.endswith(".json"):
                                        ok4, c = smb_read_file(
                                            server, share, f"{metadata_base}/agents/{f}",
                                            username, password, domain
                                        )
                                        if ok4:
                                            try:
                                                agent = json_module.loads(c)
                                                agent_id = agent.get("agent_id")
                                                if agent_id and agent_id not in agent_ids_seen:
                                                    agent_ids_seen.add(agent_id)
                                                    all_agents.append(agent)
                                            except json_module.JSONDecodeError:
                                                pass

                            # Read jobs
                            ok3, job_dirs = smb_list_files(
                                server, share, f"{metadata_base}/jobs", username, password, domain
                            )
                            if ok3:
                                for d in job_dirs:
                                    ok4, c = smb_read_file(
                                        server, share, f"{metadata_base}/jobs/{d}/config.json",
                                        username, password, domain
                                    )
                                    if ok4:
                                        try:
                                            job = json_module.loads(c)
                                            # Add job_folder context
                                            job["job_folder"] = job_folder
                                            ok5, runs = smb_list_files(
                                                server, share, f"{metadata_base}/jobs/{d}/runs",
                                                username, password, domain
                                            )
                                            run_count = len([r for r in runs if r.endswith(".json")]) if ok5 else 0
                                            job["run_count"] = run_count
                                            all_jobs.append(job)
                                        except json_module.JSONDecodeError:
                                            pass

                            # Read snapshots
                            ok3, snap_files = smb_list_files(
                                server, share, f"{metadata_base}/snapshots", username, password, domain
                            )
                            if ok3:
                                for f in snap_files:
                                    if f.endswith(".json"):
                                        ok4, c = smb_read_file(
                                            server, share, f"{metadata_base}/snapshots/{f}",
                                            username, password, domain
                                        )
                                        if ok4:
                                            try:
                                                snap = json_module.loads(c)
                                                snap["job_folder"] = job_folder
                                                all_snapshots.append(snap)
                                            except json_module.JSONDecodeError:
                                                pass

                    # Also scan Hypervisors folder specifically for VM backups
                    all_hypervisors = []
                    all_guests = []
                    hypervisors_path = f"{base_path}/Hypervisors" if base_path else "Hypervisors"
                    ok_hv, hv_folders = smb_list_files(
                        server, share, hypervisors_path,
                        username, password, domain
                    )
                    if ok_hv:
                        for hv_folder in hv_folders:
                            if hv_folder.startswith('.'):
                                continue
                            task.message = f"Scanning hypervisor: {hv_folder}..."
                            hv_metadata_base = f"{hypervisors_path}/{hv_folder}/.backer"

                            # Read hypervisor metadata
                            ok_meta, meta_content = smb_read_file(
                                server, share, f"{hv_metadata_base}/metadata.json",
                                username, password, domain
                            )
                            if ok_meta:
                                found_any_metadata = True
                                logger.info(f"[SCAN] Found hypervisor metadata: {hv_folder}")

                                # Read hypervisors
                                ok_hvs, hv_files = smb_list_files(
                                    server, share, f"{hv_metadata_base}/hypervisors",
                                    username, password, domain
                                )
                                if ok_hvs:
                                    for f in hv_files:
                                        if f.endswith(".json"):
                                            ok_h, c = smb_read_file(
                                                server, share, f"{hv_metadata_base}/hypervisors/{f}",
                                                username, password, domain
                                            )
                                            if ok_h:
                                                try:
                                                    hv_data = json_module.loads(c)
                                                    hv_data["folder"] = hv_folder
                                                    all_hypervisors.append(hv_data)
                                                except json_module.JSONDecodeError:
                                                    pass

                                # Read guests from hypervisor_backups
                                ok_guests, guest_dirs = smb_list_files(
                                    server, share, f"{hv_metadata_base}/hypervisor_backups",
                                    username, password, domain
                                )
                                if ok_guests:
                                    for vmid_dir in guest_dirs:
                                        ok_g, g_content = smb_read_file(
                                            server, share, f"{hv_metadata_base}/hypervisor_backups/{vmid_dir}/guest.json",
                                            username, password, domain
                                        )
                                        if ok_g:
                                            try:
                                                guest_data = json_module.loads(g_content)
                                                guest_data["hypervisor_folder"] = hv_folder
                                                # Count backup runs
                                                ok_runs, run_files = smb_list_files(
                                                    server, share, f"{hv_metadata_base}/hypervisor_backups/{vmid_dir}/runs",
                                                    username, password, domain
                                                )
                                                guest_data["run_count"] = len([r for r in run_files if r.endswith(".json")]) if ok_runs else 0
                                                all_guests.append(guest_data)
                                            except json_module.JSONDecodeError:
                                                pass

                    if not found_any_metadata:
                        # Also check for legacy metadata at root level (backwards compatibility)
                        metadata_base = f"{base_path}/.backer" if base_path else ".backer"
                        ok, content = smb_read_file(
                            server, share, f"{metadata_base}/metadata.json",
                            username, password, domain
                        )
                        if not ok:
                            logger.info(f"[SCAN] No metadata found in //{server}/{share}/{base_path}")
                            return format_result({
                                "initialized": False,
                                "agents": [],
                                "jobs": [],
                                "snapshots": [],
                                "summary": {"agent_count": 0, "job_count": 0, "snapshot_count": 0, "total_runs": 0},
                                "scan_path": f"//{server}/{share}/{base_path}",
                                "scan_note": "Scanned job subfolders for .backer/metadata.json - none found",
                            })

                    task.progress = 90
                    return format_result({
                        "initialized": found_any_metadata,
                        "agents": all_agents,
                        "jobs": all_jobs,
                        "snapshots": all_snapshots,
                        "hypervisors": all_hypervisors,
                        "guests": all_guests,
                        "summary": {
                            "agent_count": len(all_agents),
                            "job_count": len(all_jobs),
                            "snapshot_count": len(all_snapshots),
                            "hypervisor_count": len(all_hypervisors),
                            "guest_count": len(all_guests),
                            "total_runs": sum(j.get("run_count", 0) for j in all_jobs),
                            "total_vm_runs": sum(g.get("run_count", 0) for g in all_guests),
                        },
                    })

                else:
                    return {
                        "success": False,
                        "error": "NFS scanning requires mount_point to be set",
                        "repository_id": repo_id,
                        "repository_name": repo_name,
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

        task_manager = get_task_manager()
        task = task_manager.submit(
            task_type="scan_repository",
            description=f"Scanning repository '{repo_name}'",
            func=scan_repo_task,
        )

        return {"task_id": task.id, "status": "scanning", "message": "Repository scan started"}

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

    # ============ Hypervisor Management ============

    @app.get("/api/v1/hypervisors")
    def list_hypervisors(
        hypervisor_type: str | None = None,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List all hypervisors."""
        return storage.list_hypervisors(hypervisor_type)

    @app.post("/api/v1/hypervisors")
    async def create_hypervisor(
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Add a new hypervisor."""
        # Get request body
        body = await request.json()

        name = body.get("name", "").strip()
        hypervisor_type = body.get("hypervisor_type", "proxmox")
        host = body.get("host", "").strip()
        port = body.get("port", 8006)
        auth_method = body.get("auth_method", "token")
        username = body.get("username")
        token_id = body.get("token_id")
        token_secret = body.get("token_secret")
        password = body.get("password")
        verify_ssl = body.get("verify_ssl", False)

        # SSH settings for incremental backups
        ssh_user = body.get("ssh_user", "root")
        ssh_port = body.get("ssh_port", 22)
        ssh_key_path = body.get("ssh_key_path")
        ssh_use_api_password = body.get("ssh_use_api_password", True)

        # Validate
        validate_name(name, "name")
        if not host:
            raise HTTPException(status_code=400, detail="Host is required")

        if auth_method == "token" and (not token_id or not token_secret):
            raise HTTPException(status_code=400, detail="Token ID and secret required for token auth")
        if auth_method == "password" and (not username or not password):
            raise HTTPException(status_code=400, detail="Username and password required for password auth")

        # Check for duplicate name
        if storage.get_hypervisor_by_name(name):
            raise HTTPException(status_code=400, detail="Hypervisor with this name already exists")

        hypervisor_id = str(uuid4())

        storage.add_hypervisor(
            hypervisor_id=hypervisor_id,
            name=name,
            hypervisor_type=hypervisor_type,
            host=host,
            port=port,
            auth_method=auth_method,
            username=username,
            token_id=token_id,
            token_secret=token_secret,
            password=password,
            verify_ssl=verify_ssl,
            ssh_user=ssh_user,
            ssh_port=ssh_port,
            ssh_key_path=ssh_key_path,
            ssh_use_api_password=ssh_use_api_password,
        )

        return {"id": hypervisor_id, "name": name, "status": "created"}

    @app.post("/api/v1/hypervisors/test")
    async def test_hypervisor_credentials(
        request: Request,
    ) -> dict[str, Any]:
        """Test hypervisor connection without saving.

        This is used to validate credentials before creating a hypervisor.
        """
        from backer.hypervisors.proxmox import ProxmoxAPI, ProxmoxAuthMethod

        body = await request.json()

        hypervisor_type = body.get("hypervisor_type", "proxmox")
        host = body.get("host", "").strip()
        port = body.get("port", 8006)
        auth_method = body.get("auth_method", "token")
        username = body.get("username")
        token_id = body.get("token_id")
        token_secret = body.get("token_secret")
        password = body.get("password")
        verify_ssl = body.get("verify_ssl", False)

        if not host:
            return {"success": False, "message": "Host is required"}

        if hypervisor_type != "proxmox":
            return {"success": False, "message": f"Unsupported hypervisor type: {hypervisor_type}"}

        try:
            pve_auth = ProxmoxAuthMethod.TOKEN if auth_method == "token" else ProxmoxAuthMethod.PASSWORD

            api = ProxmoxAPI(
                host=host,
                port=port,
                auth_method=pve_auth,
                username=username,
                token_id=token_id,
                token_secret=token_secret,
                password=password,
                verify_ssl=verify_ssl,
            )

            success, message = api.test_connection()

            return {
                "success": success,
                "message": message,
                "version": api.version if success else None,
            }

        except Exception as e:
            logger.exception("Failed to test hypervisor connection")
            return {"success": False, "message": str(e)}

    @app.get("/api/v1/hypervisors/{hypervisor_id}")
    def get_hypervisor(
        hypervisor_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Get hypervisor details."""
        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")
        return hypervisor

    @app.put("/api/v1/hypervisors/{hypervisor_id}")
    async def update_hypervisor(
        hypervisor_id: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Update a hypervisor."""
        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        body = await request.json()

        # Only update fields that are provided
        update_kwargs: dict[str, Any] = {}
        if "name" in body:
            validate_name(body["name"], "name")
            update_kwargs["name"] = body["name"]
        if "host" in body:
            update_kwargs["host"] = body["host"]
        if "port" in body:
            update_kwargs["port"] = body["port"]
        if "auth_method" in body:
            update_kwargs["auth_method"] = body["auth_method"]
        if "username" in body:
            update_kwargs["username"] = body["username"]
        if "token_id" in body:
            update_kwargs["token_id"] = body["token_id"]
        if "token_secret" in body:
            update_kwargs["token_secret"] = body["token_secret"]
        if "password" in body:
            update_kwargs["password"] = body["password"]
        if "verify_ssl" in body:
            update_kwargs["verify_ssl"] = body["verify_ssl"]

        storage.update_hypervisor(hypervisor_id, **update_kwargs)

        return {"id": hypervisor_id, "status": "updated"}

    @app.delete("/api/v1/hypervisors/{hypervisor_id}")
    def delete_hypervisor(
        hypervisor_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Delete a hypervisor and its jobs."""
        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        storage.delete_hypervisor(hypervisor_id)
        return {"id": hypervisor_id, "status": "deleted"}

    @app.post("/api/v1/hypervisors/{hypervisor_id}/test")
    def test_hypervisor_connection(
        hypervisor_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Test connection to a hypervisor."""
        from backer.hypervisors.proxmox import ProxmoxAPI, ProxmoxAuthMethod

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        if hypervisor["hypervisor_type"] != "proxmox":
            raise HTTPException(status_code=400, detail="Only Proxmox hypervisors supported currently")

        # Get credentials
        token_secret = storage.get_hypervisor_token_secret(hypervisor_id)
        password = storage.get_hypervisor_password(hypervisor_id)

        auth_method = ProxmoxAuthMethod.TOKEN if hypervisor["auth_method"] == "token" else ProxmoxAuthMethod.PASSWORD

        api = ProxmoxAPI(
            host=hypervisor["host"],
            port=hypervisor["port"],
            token_id=hypervisor.get("token_id"),
            token_secret=token_secret,
            username=hypervisor.get("username"),
            password=password,
            auth_method=auth_method,
            verify_ssl=hypervisor.get("verify_ssl", False),
        )

        success, message = api.test_connection()

        # Update hypervisor status
        if success:
            storage.update_hypervisor(
                hypervisor_id,
                status="connected",
                version=message,
            )
        else:
            storage.update_hypervisor(hypervisor_id, status="error")

        return {
            "success": success,
            "message": message,
            "hypervisor_id": hypervisor_id,
        }

    @app.get("/api/v1/hypervisors/{hypervisor_id}/guests")
    def list_hypervisor_guests(
        hypervisor_id: str,
        node: str | None = None,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List VMs and containers on a hypervisor."""
        from backer.hypervisors.proxmox import ProxmoxAPI, ProxmoxAuthMethod

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        if hypervisor["hypervisor_type"] != "proxmox":
            raise HTTPException(status_code=400, detail="Only Proxmox hypervisors supported currently")

        token_secret = storage.get_hypervisor_token_secret(hypervisor_id)
        password = storage.get_hypervisor_password(hypervisor_id)

        auth_method = ProxmoxAuthMethod.TOKEN if hypervisor["auth_method"] == "token" else ProxmoxAuthMethod.PASSWORD

        api = ProxmoxAPI(
            host=hypervisor["host"],
            port=hypervisor["port"],
            token_id=hypervisor.get("token_id"),
            token_secret=token_secret,
            username=hypervisor.get("username"),
            password=password,
            auth_method=auth_method,
            verify_ssl=hypervisor.get("verify_ssl", False),
        )

        try:
            # Authenticate if using password-based auth
            if auth_method == ProxmoxAuthMethod.PASSWORD:
                api.authenticate()

            guests = api.list_guests(node)
            return [
                {
                    "vmid": g.vmid,
                    "name": g.name,
                    "node": g.node,
                    "type": g.guest_type.value,
                    "status": g.status,
                    "cpus": g.cpus,
                    "maxmem_gb": round(g.maxmem_gb, 2),
                    "maxdisk_gb": round(g.maxdisk_gb, 2),
                    "template": g.template,
                    "tags": g.tags,
                }
                for g in guests
            ]
        except Exception as e:
            logger.exception(f"Failed to list guests for hypervisor {hypervisor_id}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/v1/hypervisors/{hypervisor_id}/nodes")
    def list_hypervisor_nodes(
        hypervisor_id: str,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List nodes on a hypervisor cluster."""
        from backer.hypervisors.proxmox import ProxmoxAPI, ProxmoxAuthMethod

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        token_secret = storage.get_hypervisor_token_secret(hypervisor_id)
        password = storage.get_hypervisor_password(hypervisor_id)

        auth_method = ProxmoxAuthMethod.TOKEN if hypervisor["auth_method"] == "token" else ProxmoxAuthMethod.PASSWORD

        api = ProxmoxAPI(
            host=hypervisor["host"],
            port=hypervisor["port"],
            token_id=hypervisor.get("token_id"),
            token_secret=token_secret,
            username=hypervisor.get("username"),
            password=password,
            auth_method=auth_method,
            verify_ssl=hypervisor.get("verify_ssl", False),
        )

        try:
            # Authenticate if using password-based auth
            if auth_method == ProxmoxAuthMethod.PASSWORD:
                api.authenticate()

            nodes = api.list_nodes()
            return [
                {
                    "node": n.node,
                    "status": n.status,
                    "cpu_percent": round(n.cpu, 1),
                    "maxcpu": n.maxcpu,
                    "mem_used_gb": round(n.mem / (1024**3), 2),
                    "mem_total_gb": round(n.maxmem / (1024**3), 2),
                    "uptime_hours": round(n.uptime / 3600, 1),
                }
                for n in nodes
            ]
        except Exception as e:
            logger.exception(f"Failed to list nodes for hypervisor {hypervisor_id}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/v1/hypervisors/{hypervisor_id}/storages")
    def list_hypervisor_storages(
        hypervisor_id: str,
        node: str | None = None,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List backup-capable storages on a hypervisor."""
        from backer.hypervisors.proxmox import ProxmoxAPI, ProxmoxAuthMethod

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        token_secret = storage.get_hypervisor_token_secret(hypervisor_id)
        password = storage.get_hypervisor_password(hypervisor_id)

        auth_method = ProxmoxAuthMethod.TOKEN if hypervisor["auth_method"] == "token" else ProxmoxAuthMethod.PASSWORD

        api = ProxmoxAPI(
            host=hypervisor["host"],
            port=hypervisor["port"],
            token_id=hypervisor.get("token_id"),
            token_secret=token_secret,
            username=hypervisor.get("username"),
            password=password,
            auth_method=auth_method,
            verify_ssl=hypervisor.get("verify_ssl", False),
        )

        try:
            # Authenticate if using password-based auth
            if auth_method == ProxmoxAuthMethod.PASSWORD:
                api.authenticate()

            storages = api.list_storages(node)
            return [
                {
                    "storage": s.storage,
                    "type": s.type,
                    "node": s.node,
                    "content": s.content,
                    "path": s.path,
                    "active": s.active,
                    "enabled": s.enabled,
                    "shared": s.shared,
                    "total": s.total,
                    "used": s.used,
                    "avail": s.avail,
                }
                for s in storages
            ]
        except Exception as e:
            logger.exception(f"Failed to list storages for hypervisor {hypervisor_id}")
            raise HTTPException(status_code=500, detail=str(e))

    # ============ Hypervisor Jobs ============

    @app.get("/api/v1/hypervisor-jobs")
    def list_hypervisor_jobs(
        hypervisor_id: str | None = None,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List hypervisor backup jobs."""
        jobs = storage.list_hypervisor_jobs(hypervisor_id)

        # Enhance with hypervisor info and latest run
        for job in jobs:
            hypervisor = storage.get_hypervisor(job["hypervisor_id"])
            if hypervisor:
                job["hypervisor_name"] = hypervisor["name"]
                job["hypervisor_type"] = hypervisor["hypervisor_type"]

            latest = storage.get_latest_hypervisor_run(job["id"])
            job["last_status"] = latest["status"] if latest else None
            job["last_run"] = latest["started_at"] if latest else None

            # Add guest count for UI
            job["guest_count"] = len(job.get("guest_ids") or [])

        return jobs

    @app.post("/api/v1/hypervisor-jobs")
    async def create_hypervisor_job(
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Create a new hypervisor backup job."""
        body = await request.json()

        name = body.get("name", "").strip()
        hypervisor_id = body.get("hypervisor_id")
        # Accept both vmids and guest_ids
        guest_ids = body.get("vmids") or body.get("guest_ids") or []
        # Repository ID for Backer storage
        repository_id = body.get("repository_id")
        # Accept both mode/backup_mode and compress/compression
        backup_mode = body.get("mode") or body.get("backup_mode", "snapshot")
        compression = body.get("compress") or body.get("compression", "zstd")
        schedule_cron = body.get("schedule_cron")
        retention = body.get("retention", {})
        enabled = body.get("enabled", True)

        # Incremental backup settings
        enable_incremental = body.get("enable_incremental", False)
        ssh_user = body.get("ssh_user", "root")
        ssh_port = body.get("ssh_port", 22)
        max_incrementals = body.get("max_incrementals", 7)

        # Validate
        validate_name(name, "name")
        if not hypervisor_id:
            raise HTTPException(status_code=400, detail="hypervisor_id is required")
        # guest_ids can be empty to backup all guests
        if not repository_id:
            raise HTTPException(status_code=400, detail="repository_id is required")

        # Check hypervisor exists
        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        # Check repository exists and is SMB/NFS
        repository = storage.get_repository(repository_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        repo_type = repository.get("repo_type", "").lower()
        if repo_type not in ("smb", "nfs"):
            raise HTTPException(
                status_code=400,
                detail=f"Repository type '{repo_type}' is not supported for hypervisor backups. "
                       "Only SMB or NFS repositories can be used."
            )

        # Check for duplicate name
        if storage.get_hypervisor_job_by_name(name):
            raise HTTPException(status_code=400, detail="Job with this name already exists")

        job_id = str(uuid4())

        storage.add_hypervisor_job(
            job_id=job_id,
            name=name,
            hypervisor_id=hypervisor_id,
            guest_ids=guest_ids,
            repository_id=repository_id,
            backup_mode=backup_mode,
            compression=compression,
            schedule_cron=schedule_cron,
            retention=retention,
            enabled=enabled,
            enable_incremental=enable_incremental,
            ssh_user=ssh_user,
            ssh_port=ssh_port,
            max_incrementals=max_incrementals,
        )

        return {"id": job_id, "name": name, "status": "created"}

    @app.get("/api/v1/hypervisor-jobs/{job_id}")
    def get_hypervisor_job(
        job_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Get hypervisor job details."""
        job = storage.get_hypervisor_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    @app.put("/api/v1/hypervisor-jobs/{job_id}")
    async def update_hypervisor_job(
        job_id: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Update a hypervisor job."""
        job = storage.get_hypervisor_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        body = await request.json()

        update_kwargs: dict[str, Any] = {}
        if "name" in body:
            validate_name(body["name"], "name")
            update_kwargs["name"] = body["name"]
        # Accept both vmids and guest_ids
        if "vmids" in body or "guest_ids" in body:
            update_kwargs["guest_ids"] = body.get("vmids") or body.get("guest_ids")
        # Repository ID for Backer storage
        if "repository_id" in body:
            new_repo_id = body["repository_id"]
            repository = storage.get_repository(new_repo_id)
            if not repository:
                raise HTTPException(status_code=404, detail="Repository not found")
            repo_type = repository.get("repo_type", "").lower()
            if repo_type not in ("smb", "nfs"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Repository type '{repo_type}' is not supported for hypervisor backups. "
                           "Only SMB or NFS repositories can be used."
                )
            update_kwargs["repository_id"] = new_repo_id
        # Accept both mode and backup_mode
        if "mode" in body or "backup_mode" in body:
            update_kwargs["backup_mode"] = body.get("mode") or body.get("backup_mode")
        # Accept both compress and compression
        if "compress" in body or "compression" in body:
            update_kwargs["compression"] = body.get("compress") or body.get("compression")
        if "schedule_cron" in body:
            update_kwargs["schedule_cron"] = body["schedule_cron"]
        if "retention" in body:
            update_kwargs["retention"] = body["retention"]
        if "enabled" in body:
            update_kwargs["enabled"] = body["enabled"]
        # Incremental backup settings
        if "enable_incremental" in body:
            update_kwargs["enable_incremental"] = body["enable_incremental"]
        if "ssh_user" in body:
            update_kwargs["ssh_user"] = body["ssh_user"]
        if "ssh_port" in body:
            update_kwargs["ssh_port"] = body["ssh_port"]
        if "max_incrementals" in body:
            update_kwargs["max_incrementals"] = body["max_incrementals"]

        storage.update_hypervisor_job(job_id, **update_kwargs)

        return {"id": job_id, "status": "updated"}

    @app.delete("/api/v1/hypervisor-jobs/{job_id}")
    def delete_hypervisor_job(
        job_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Delete a hypervisor job and clean up all repository data.

        This permanently deletes the job configuration, all backup files,
        and all metadata from the repository. This prevents conflicts when
        creating new jobs with the same names or VMIDs.
        """
        job = storage.get_hypervisor_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        cleanup_errors = []

        # Always clean up repository data to prevent conflicts
        repository_id = job.get("repository_id")
        hypervisor_id = job.get("hypervisor_id")

        if repository_id and hypervisor_id:
            repository = storage.get_repository(repository_id)
            hypervisor = storage.get_hypervisor(hypervisor_id)

            if repository and hypervisor:
                try:
                    _cleanup_hypervisor_job_data(
                        repository=repository,
                        hypervisor=hypervisor,
                        storage=storage,
                        repository_id=repository_id,
                    )
                    logger.info(f"Cleaned up repository data for job {job_id}")
                except Exception as e:
                    logger.warning(f"Failed to clean up repository data: {e}")
                    cleanup_errors.append(str(e))

        # Delete local database records
        storage.delete_hypervisor_job(job_id)

        result: dict[str, Any] = {"id": job_id, "status": "deleted"}
        if cleanup_errors:
            result["cleanup_warnings"] = cleanup_errors
        return result

    def _cleanup_hypervisor_job_data(
        repository: dict[str, Any],
        hypervisor: dict[str, Any],
        storage: Storage,
        repository_id: str,
    ) -> None:
        """Clean up all job data from the repository.

        Deletes:
        - Backup files in dump/
        - Metadata in .backer/
        - The hypervisor folder if empty after cleanup
        """
        import subprocess

        repo_type = repository.get("repo_type", "").lower()
        if repo_type != "smb":
            logger.debug(f"Repository cleanup not supported for type: {repo_type}")
            return

        server = repository.get("server", "")
        share = repository.get("share", "")
        subdir = repository.get("path", "").strip("/")
        username = repository.get("username")
        domain = repository.get("domain")

        # Get repository password
        repo_password = storage.get_repository_password(repository_id)

        if not server or not share:
            logger.warning("Cannot clean up: missing server or share")
            return

        # Build hypervisor folder path
        safe_hv_name = "".join(
            c if c.isalnum() or c in "-_ " else "_" for c in hypervisor["name"]
        )
        if subdir:
            hv_path = f"{subdir}/Hypervisors/{safe_hv_name}"
        else:
            hv_path = f"Hypervisors/{safe_hv_name}"

        # Build smbclient auth
        if username:
            if domain:
                auth_str = f"{domain}\\{username}%{repo_password or ''}"
            else:
                auth_str = f"{username}%{repo_password or ''}"
            auth_parts = ["-U", auth_str]
        else:
            auth_parts = ["-N"]

        def run_smb_cmd(cmd: str) -> tuple[bool, str]:
            """Run an smbclient command and return success status and output."""
            full_cmd = ["smbclient", f"//{server}/{share}", *auth_parts, "-c", cmd]
            try:
                result = subprocess.run(
                    full_cmd, capture_output=True, timeout=60, text=True
                )
                return result.returncode == 0, result.stderr or result.stdout
            except subprocess.TimeoutExpired:
                return False, "Command timed out"
            except Exception as e:
                return False, str(e)

        # Step 1: Delete all files in dump/ folder
        dump_path = f"{hv_path}/dump"
        logger.info(f"Cleaning up backup files in {dump_path}")

        # List files first
        ok, output = run_smb_cmd(f'ls "{dump_path}/*"')
        if ok:
            # Delete each vzdump file
            for line in output.split("\n"):
                if "vzdump-" in line:
                    # Extract filename from smbclient ls output
                    parts = line.strip().split()
                    if parts:
                        filename = parts[0]
                        if filename.startswith("vzdump-"):
                            del_ok, del_out = run_smb_cmd(
                                f'del "{dump_path}/{filename}"'
                            )
                            if del_ok:
                                logger.debug(f"Deleted backup: {filename}")
                            else:
                                logger.warning(f"Failed to delete {filename}: {del_out}")

        # Step 2: Delete metadata folder contents
        backer_path = f"{hv_path}/.backer"
        logger.info(f"Cleaning up metadata in {backer_path}")

        # Delete files in subdirectories
        for subpath in ["hypervisors", "guests", "runs", "jobs"]:
            full_path = f"{backer_path}/{subpath}"
            ok, output = run_smb_cmd(f'ls "{full_path}/*"')
            if ok:
                for line in output.split("\n"):
                    if ".json" in line:
                        parts = line.strip().split()
                        if parts and parts[0].endswith(".json"):
                            filename = parts[0]
                            run_smb_cmd(f'del "{full_path}/{filename}"')

            # Try to remove the empty directory
            run_smb_cmd(f'rmdir "{full_path}"')

        # Delete metadata.json
        run_smb_cmd(f'del "{backer_path}/metadata.json"')

        # Try to remove .backer directory
        run_smb_cmd(f'rmdir "{backer_path}"')

        # Step 3: Try to remove dump directory if empty
        run_smb_cmd(f'rmdir "{dump_path}"')

        # Step 4: Try to remove hypervisor folder if empty
        run_smb_cmd(f'rmdir "{hv_path}"')

        logger.info(f"Repository cleanup completed for hypervisor: {hypervisor['name']}")

    def _write_hypervisor_backup_metadata(
        repository: dict[str, Any],
        hypervisor: dict[str, Any],
        job: dict[str, Any],
        job_id: str,
        run_id: str,
        results: list[dict[str, Any]],
        guest_map: dict[int, Any],
    ) -> None:
        """Write backup metadata to the repository using smbclient.

        This allows the metadata to be discovered if the Backer server is reinstalled.
        Uses smbclient to write directly to SMB shares without requiring mount.
        """
        import subprocess
        import tempfile
        from backer.hypervisors.metadata import HypervisorMetadata

        repo_type = repository.get("repo_type", "").lower()
        if repo_type not in ("smb", "nfs"):
            logger.debug(f"Skipping metadata write for repo type: {repo_type}")
            return

        server = repository.get("server", "")
        share = repository.get("share", "")
        subdir = repository.get("path", "")
        username = repository.get("username")
        password = repository.get("password")
        domain = repository.get("domain")

        if not server or not share:
            logger.warning("Cannot write metadata: missing server or share")
            return

        # Create metadata in a temp directory first
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            metadata = HypervisorMetadata(tmp_path)

            # Initialize if needed
            if not metadata.is_initialized():
                metadata.initialize()

            # Save hypervisor info
            metadata.save_hypervisor(
                hypervisor_id=hypervisor["id"],
                name=hypervisor["name"],
                hypervisor_type=hypervisor.get("hypervisor_type", "proxmox"),
                host=hypervisor["host"],
            )

            # Save guest and run info for each result
            for result in results:
                vmid = result.get("vmid")
                if not vmid:
                    continue

                guest = guest_map.get(vmid)
                guest_name = guest.name if guest else f"VM {vmid}"
                guest_type = guest.guest_type.value if guest else "qemu"
                node = guest.node if guest else "unknown"

                # Save guest info
                metadata.save_guest(
                    vmid=vmid,
                    name=guest_name,
                    guest_type=guest_type,
                    node=node,
                    hypervisor_id=hypervisor["id"],
                )

                # Save run record
                metadata.save_backup_run(
                    vmid=vmid,
                    run_id=run_id,
                    status="success" if result.get("success") else "failed",
                    backup_file=result.get("archive_name", ""),
                    started_at=result.get("started_at", datetime.now().isoformat()),
                    finished_at=result.get("finished_at"),
                    size_bytes=result.get("archive_size"),
                    duration_seconds=result.get("duration_seconds"),
                    backup_type=result.get("backup_type", "full"),
                    skipped=result.get("skipped", False),
                    job_name=job["name"],
                    job_id=job_id,
                    hypervisor_id=hypervisor["id"],
                )

            # Now upload the .backer directory to the SMB share
            backer_dir = tmp_path / ".backer"
            if not backer_dir.exists():
                return

            # Build remote path: {repo_path}/Hypervisors/{hypervisor_name}/
            base_path = subdir.strip("/") if subdir else ""
            # Sanitize hypervisor name for folder
            safe_hv_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in hypervisor["name"])
            if base_path:
                remote_base = f"{base_path}/Hypervisors/{safe_hv_name}"
            else:
                remote_base = f"Hypervisors/{safe_hv_name}"

            # Build smbclient auth
            auth_parts = []
            if username:
                auth_parts.extend(["-U", f"{domain}\\{username}%{password}" if domain else f"{username}%{password}"])
            else:
                auth_parts.extend(["-N"])  # No password

            # Track directories we've already created to avoid redundant mkdir calls
            created_dirs: set[str] = set()

            def ensure_remote_dir(dir_path: str) -> None:
                """Create remote directory and all parents using smbclient."""
                if not dir_path or dir_path in created_dirs:
                    return

                # Build list of directories to create (from root to leaf)
                parts = dir_path.split("/")
                for i in range(1, len(parts) + 1):
                    partial_path = "/".join(parts[:i])
                    if partial_path and partial_path not in created_dirs:
                        mkdir_cmd = ["smbclient", f"//{server}/{share}", *auth_parts, "-c", f"mkdir {partial_path}"]
                        subprocess.run(mkdir_cmd, capture_output=True, timeout=30)
                        created_dirs.add(partial_path)

            # Upload each file in .backer directory
            for local_file in backer_dir.rglob("*"):
                if not local_file.is_file():
                    continue

                # rel_path is relative to backer_dir (e.g., "hypervisors/123.json" or "metadata.json")
                rel_path = local_file.relative_to(backer_dir)
                rel_path_str = str(rel_path).replace("\\", "/")

                # Build remote directory path
                # For "metadata.json" -> parent is "." -> remote_dir is just "{remote_base}/.backer"
                # For "hypervisors/123.json" -> remote_dir is "{remote_base}/.backer/hypervisors"
                parent_str = str(rel_path.parent).replace("\\", "/")
                if parent_str == ".":
                    remote_dir = f"{remote_base}/.backer"
                else:
                    remote_dir = f"{remote_base}/.backer/{parent_str}"

                # Create remote directory (and all parents)
                ensure_remote_dir(remote_dir)

                # Upload file
                remote_file = f"{remote_base}/.backer/{rel_path_str}"
                put_cmd = [
                    "smbclient", f"//{server}/{share}", *auth_parts,
                    "-c", f"put {local_file} {remote_file}"
                ]
                result = subprocess.run(put_cmd, capture_output=True, timeout=30)
                if result.returncode != 0:
                    logger.debug(f"smbclient put failed for {remote_file}: {result.stderr.decode()}")
                else:
                    logger.debug(f"Uploaded metadata: {remote_file}")

            logger.info(f"Wrote hypervisor backup metadata to //{server}/{share}/{remote_base}")

    @app.post("/api/v1/hypervisor-jobs/{job_id}/run")
    def run_hypervisor_job(
        job_id: str,
        force_full: bool = False,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Run a hypervisor backup job.

        Backups are stored directly on the Backer repository by auto-configuring
        the repository as Proxmox storage (like Veeam does).

        When incremental backups are enabled for the job, uses QEMU dirty bitmaps
        to determine which VMs have changed. VMs with no changes can be skipped.

        Args:
            job_id: Hypervisor job ID
            force_full: Force full backups for all VMs (ignore dirty bitmap state)
        """
        from backer.hypervisors.incremental import (
            BackupType,
            IncrementalBackupManager,
        )
        from backer.hypervisors.proxmox import (
            ProxmoxAPI,
            ProxmoxAPIError,
            ProxmoxAuthMethod,
            ProxmoxBackupManager,
            ProxmoxBackupMode,
            ProxmoxCompression,
        )

        job = storage.get_hypervisor_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        hypervisor = storage.get_hypervisor(job["hypervisor_id"])
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        if hypervisor["hypervisor_type"] != "proxmox":
            raise HTTPException(status_code=400, detail="Only Proxmox hypervisors supported")

        # Get repository for backup destination
        repository_id = job.get("repository_id")
        if not repository_id:
            raise HTTPException(status_code=400, detail="No repository configured for this job")

        repository = storage.get_repository(repository_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        # Validate repository type (must be SMB or NFS for Proxmox storage)
        repo_type = repository.get("repo_type", "").lower()
        if repo_type not in ("smb", "nfs"):
            raise HTTPException(
                status_code=400,
                detail=f"Repository type '{repo_type}' is not supported for hypervisor backups. "
                       "Use an SMB or NFS repository so Proxmox can write directly to it."
            )

        # Get credentials
        token_secret = storage.get_hypervisor_token_secret(hypervisor["id"])
        hv_password = storage.get_hypervisor_password(hypervisor["id"])

        auth_method = ProxmoxAuthMethod.TOKEN if hypervisor["auth_method"] == "token" else ProxmoxAuthMethod.PASSWORD

        api = ProxmoxAPI(
            host=hypervisor["host"],
            port=hypervisor["port"],
            token_id=hypervisor.get("token_id"),
            token_secret=token_secret,
            username=hypervisor.get("username"),
            password=hv_password,
            auth_method=auth_method,
            verify_ssl=hypervisor.get("verify_ssl", False),
        )

        # Authenticate if using password-based auth
        if auth_method == ProxmoxAuthMethod.PASSWORD:
            api.authenticate()

        # Get repository password for SMB storage
        repo_password = None
        if repo_type == "smb" and repository.get("has_password"):
            repo_password = storage.get_repository_password(repository_id)

        # Create repository dict with password for ensure_backer_storage
        repo_with_password = {**repository, "password": repo_password}

        run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

        # Get SSH credentials - used for both incremental backups and cleanup operations
        # Use job-level SSH settings, fall back to hypervisor settings
        ssh_user = job.get("ssh_user") or hypervisor.get("ssh_user", "root")
        ssh_port = job.get("ssh_port") or hypervisor.get("ssh_port", 22)
        ssh_key_path = hypervisor.get("ssh_key_path")

        # For SSH password: use API password if ssh_use_api_password is enabled
        ssh_password = None
        if hypervisor.get("ssh_use_api_password", True) and hv_password:
            ssh_password = hv_password

        # Set up incremental backup manager if enabled
        incremental_manager = None
        enable_incremental = job.get("enable_incremental", False)
        logger.info(f"Job '{job.get('name')}' enable_incremental={enable_incremental}")

        if enable_incremental:
            incremental_manager = IncrementalBackupManager(
                host=hypervisor["host"],
                hypervisor_id=hypervisor["id"],
                storage=storage,
                ssh_user=ssh_user,
                ssh_port=ssh_port,
                ssh_key=ssh_key_path,
                ssh_password=ssh_password,
                max_incrementals=job.get("max_incrementals", 7),
            )

        # Submit backup as background task
        def run_backup_task(task: Task) -> dict[str, Any]:
            # Ensure Proxmox storage exists for this repository (done in background to avoid blocking API)
            # Backups go to: {repo_path}/Hypervisors/{hypervisor_name}/dump/
            task.message = "Configuring backup storage..."
            try:
                proxmox_storage_id = api.ensure_backer_storage(
                    repo_with_password,
                    hypervisor_name=hypervisor["name"],
                    ssh_user=ssh_user,
                    ssh_port=ssh_port,
                    ssh_key=ssh_key_path,
                    ssh_password=ssh_password,
                )
            except ProxmoxAPIError as e:
                # Raise the error so the task shows as FAILED, not completed
                raise RuntimeError(f"Failed to configure Proxmox storage: {e}") from e

            manager = ProxmoxBackupManager(api)
            results = []

            # Get guest names and resolve guest_ids
            try:
                logger.info("Fetching guest list from Proxmox...")
                all_guests = api.list_guests()
                guest_map = {g.vmid: g for g in all_guests}
                logger.info(f"Found {len(all_guests)} guests: {[g.vmid for g in all_guests]}")
            except Exception as e:
                logger.warning(f"Failed to list guests: {e}")
                all_guests = []
                guest_map = {}

            # If no specific guests, backup all
            guest_ids = job.get("guest_ids") or []
            logger.info(f"Job guest_ids from storage: {guest_ids} (types: {[type(x).__name__ for x in guest_ids]})")
            if not guest_ids:
                guest_ids = [g.vmid for g in all_guests]
                logger.info(f"No specific guests, backing up all: {guest_ids}")

            total = len(guest_ids)
            logger.info(f"Total guests to backup: {total}")
            if total == 0:
                logger.warning("No guests to backup - returning early")
                return {"run_id": run_id, "total": 0, "success": 0, "failed": 0, "skipped": 0, "results": []}

            # Counters for incremental stats
            skipped_count = 0

            for i, vmid in enumerate(guest_ids):
                logger.info(f"=== Processing guest {i+1}/{total}: VMID {vmid} ===")
                task.progress = int((i / total) * 100)
                guest = guest_map.get(vmid)
                guest_name = guest.name if guest else f"VM {vmid}"
                logger.info(f"Guest lookup: vmid={vmid} (type={type(vmid).__name__}), found={guest is not None}, name={guest_name}")

                # Check if we can skip this VM (incremental mode with no changes)
                backup_decision = None
                backup_type_label = "full"

                logger.info(f"Incremental check for VM {vmid}: manager={'set' if incremental_manager else 'None'}")
                if incremental_manager:
                    task.message = f"Checking changes for {guest_name} ({vmid})..."
                    logger.info(f"Checking dirty bitmaps for VM {vmid}...")
                    try:
                        backup_decision = incremental_manager.get_backup_decision(
                            vmid=vmid,
                            force_full=force_full,
                        )
                        backup_type_label = backup_decision.backup_type.value
                        logger.info(f"VM {vmid} backup decision: {backup_decision.backup_type.value} - {backup_decision.reason}")

                        if backup_decision.backup_type == BackupType.SKIP:
                            # No changes since last backup - skip this VM
                            logger.info(
                                f"Skipping VM {vmid} ({guest_name}): {backup_decision.reason}"
                            )
                            storage.save_hypervisor_run(
                                run_id=run_id,
                                job_id=job_id,
                                job_name=job["name"],
                                hypervisor_id=hypervisor["id"],
                                guest_id=vmid,
                                guest_name=guest_name,
                                status="skipped",
                                finished_at=datetime.now(),
                                exit_status="no changes",
                            )
                            results.append({
                                "success": True,
                                "vmid": vmid,
                                "skipped": True,
                                "reason": backup_decision.reason,
                            })
                            skipped_count += 1
                            continue

                    except Exception as e:
                        logger.warning(
                            f"Error checking incremental state for VM {vmid}: {e}. "
                            "Falling back to full backup."
                        )
                        backup_decision = None

                task.message = f"Backing up {guest_name} ({vmid}) [{backup_type_label}] to {repository['name']}..."

                # Record pending
                storage.save_hypervisor_run(
                    run_id=run_id,
                    job_id=job_id,
                    job_name=job["name"],
                    hypervisor_id=hypervisor["id"],
                    guest_id=vmid,
                    guest_name=guest_name,
                    status="running",
                )

                try:
                    # Map backup mode
                    mode_map = {
                        "snapshot": ProxmoxBackupMode.SNAPSHOT,
                        "stop": ProxmoxBackupMode.STOP,
                        "suspend": ProxmoxBackupMode.SUSPEND,
                    }
                    backup_mode = mode_map.get(job["backup_mode"], ProxmoxBackupMode.SNAPSHOT)

                    # Map compression
                    compress_map = {
                        "zstd": ProxmoxCompression.ZSTD,
                        "gzip": ProxmoxCompression.GZIP,
                        "lzo": ProxmoxCompression.LZO,
                        "none": ProxmoxCompression.NONE,
                    }
                    compression = compress_map.get(job["compression"], ProxmoxCompression.ZSTD)

                    # Progress callback to update task with vzdump progress
                    def progress_callback(status: Any, log_lines: list[str]) -> None:
                        import re
                        for line in log_lines:
                            # Parse vzdump progress: "INFO: 5% (1.2G of 24G)"
                            match = re.search(r"(\d+)%", line)
                            if match:
                                pct = int(match.group(1))
                                # Scale to this VM's portion of overall progress
                                base_progress = int((i / total) * 100)
                                vm_progress = int((pct / 100) * (100 / total))
                                task.progress = min(base_progress + vm_progress, 99)
                            # Also update message with latest log line
                            if "INFO:" in line:
                                short_line = line.replace("INFO:", "").strip()[:60]
                                task.message = f"Backing up {guest_name}: {short_line}"

                    # Backup directly to Proxmox storage (which points to Backer repo)
                    result = manager.backup_to_storage(
                        vmid=vmid,
                        storage=proxmox_storage_id,
                        mode=backup_mode,
                        compress=compression,
                        timeout=7200,
                        progress_callback=progress_callback,
                    )

                    # Add backup type to result
                    result["backup_type"] = backup_type_label

                    # Update run record
                    storage.save_hypervisor_run(
                        run_id=run_id,
                        job_id=job_id,
                        job_name=job["name"],
                        hypervisor_id=hypervisor["id"],
                        guest_id=vmid,
                        guest_name=guest_name,
                        status="success" if result["success"] else "failed",
                        upid=result.get("upid"),
                        finished_at=datetime.fromisoformat(result["finished_at"]),
                        duration_seconds=result.get("duration_seconds"),
                        exit_status=result.get("exit_status"),
                        errors=result.get("errors"),
                    )

                    # On success, update incremental tracking
                    if result["success"] and incremental_manager and backup_decision:
                        try:
                            # Set up tracking if this was a full backup
                            if backup_decision.backup_type == BackupType.FULL:
                                incremental_manager.setup_tracking(vmid)

                            # Update bitmap state
                            incremental_manager.after_backup_success(vmid, backup_decision)
                        except Exception as inc_err:
                            logger.warning(
                                f"Failed to update incremental state for VM {vmid}: {inc_err}"
                            )

                    results.append(result)

                except Exception as e:
                    logger.exception(f"Backup failed for VMID {vmid}: {e}")

                    # Notify incremental manager of failure
                    if incremental_manager and backup_decision:
                        try:
                            incremental_manager.after_backup_failure(vmid, backup_decision)
                        except Exception:
                            pass

                    storage.save_hypervisor_run(
                        run_id=run_id,
                        job_id=job_id,
                        job_name=job["name"],
                        hypervisor_id=hypervisor["id"],
                        guest_id=vmid,
                        guest_name=guest_name,
                        status="failed",
                        finished_at=datetime.now(),
                        errors=[str(e)],
                    )
                    results.append({"success": False, "vmid": vmid, "error": str(e)})

            task.progress = 100
            task.message = "Backup job completed"

            # Write metadata to repository
            try:
                task.message = "Writing backup metadata..."
                _write_hypervisor_backup_metadata(
                    repository=repo_with_password,
                    hypervisor=hypervisor,
                    job=job,
                    job_id=job_id,
                    run_id=run_id,
                    results=results,
                    guest_map=guest_map,
                )
            except Exception as meta_err:
                # Don't fail backup if metadata write fails
                logger.warning(f"Failed to write backup metadata: {meta_err}")

            # Clean up: remove the temporary Proxmox storage
            # This unmounts the share, keeping Proxmox UI clean
            try:
                task.message = "Cleaning up temporary storage..."
                api.delete_storage(proxmox_storage_id)
                logger.info(f"Removed temporary Proxmox storage '{proxmox_storage_id}'")
            except Exception as cleanup_err:
                # Log but don't fail the backup - cleanup is best-effort
                logger.warning(f"Failed to cleanup Proxmox storage '{proxmox_storage_id}': {cleanup_err}")

            success_count = sum(1 for r in results if r.get("success") and not r.get("skipped"))
            return {
                "run_id": run_id,
                "total": total,
                "success": success_count,
                "failed": total - success_count - skipped_count,
                "skipped": skipped_count,
                "incremental_enabled": enable_incremental,
                "results": results,
            }

        task_manager = get_task_manager()
        guest_count = len(job.get("guest_ids") or [])
        hv_name = hypervisor['name']
        repo_name = repository['name']
        if guest_count:
            desc = f"Backing up {guest_count} guests from {hv_name} to {repo_name}"
        else:
            desc = f"Backing up all guests from {hv_name} to {repo_name}"

        task = task_manager.submit(
            task_type="hypervisor_backup",
            description=desc,
            func=run_backup_task,
        )

        msg = f"Backup job started for {guest_count} guests" if guest_count else "Backup job started for all guests"
        return {
            "run_id": run_id,
            "task_id": task.id,
            "message": msg,
        }

    @app.patch("/api/v1/hypervisor-jobs/{job_id}/toggle")
    def toggle_hypervisor_job(
        job_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Toggle a hypervisor job's enabled state."""
        job = storage.get_hypervisor_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        new_enabled = not job["enabled"]
        storage.update_hypervisor_job(job_id, enabled=new_enabled)

        return {"id": job_id, "enabled": new_enabled}

    @app.get("/api/v1/hypervisor-jobs/{job_id}/runs")
    def get_hypervisor_job_runs(
        job_id: str,
        limit: int = 50,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """Get backup runs for a hypervisor job."""
        job = storage.get_hypervisor_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        return storage.get_hypervisor_runs(job_id=job_id, limit=limit)

    @app.get("/api/v1/hypervisor-runs/{run_id}")
    def get_hypervisor_run(
        run_id: str,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Get details of a specific hypervisor backup run."""
        run = storage.get_hypervisor_run_by_run_id(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return run

    # ============ Incremental Backup Status ============

    @app.get("/api/v1/hypervisors/{hypervisor_id}/incremental-status/{vmid}")
    def get_vm_incremental_status(
        hypervisor_id: str,
        vmid: int,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Get incremental backup status for a specific VM.

        Returns bitmap tracking state, dirty bytes, and backup history.
        """
        from backer.hypervisors.incremental import IncrementalBackupManager

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        # Get SSH credentials
        ssh_password = None
        if hypervisor.get("ssh_use_api_password", True):
            ssh_password = storage.get_hypervisor_password(hypervisor_id)

        inc_manager = IncrementalBackupManager(
            host=hypervisor["host"],
            hypervisor_id=hypervisor_id,
            storage=storage,
            ssh_user=hypervisor.get("ssh_user", "root"),
            ssh_port=hypervisor.get("ssh_port", 22),
            ssh_key=hypervisor.get("ssh_key_path"),
            ssh_password=ssh_password,
        )

        try:
            stats = inc_manager.get_vm_backup_stats(vmid)
            validity = inc_manager.check_bitmap_validity(vmid)
            return {
                "vmid": vmid,
                "stats": stats,
                "validity": validity,
            }
        except Exception as e:
            logger.warning(f"Error getting incremental status for VM {vmid}: {e}")
            return {
                "vmid": vmid,
                "error": str(e),
                "stats": {"tracked": False},
                "validity": {"valid": False, "reason": str(e)},
            }

    @app.post("/api/v1/hypervisors/{hypervisor_id}/incremental-setup/{vmid}")
    def setup_vm_incremental_tracking(
        hypervisor_id: str,
        vmid: int,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Set up incremental backup tracking for a VM.

        Creates dirty bitmaps on all VM disks to enable change tracking.
        Should be called after a full backup or when re-initializing tracking.
        """
        from backer.hypervisors.incremental import IncrementalBackupManager

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        # Get SSH credentials
        ssh_password = None
        if hypervisor.get("ssh_use_api_password", True):
            ssh_password = storage.get_hypervisor_password(hypervisor_id)

        inc_manager = IncrementalBackupManager(
            host=hypervisor["host"],
            hypervisor_id=hypervisor_id,
            storage=storage,
            ssh_user=hypervisor.get("ssh_user", "root"),
            ssh_port=hypervisor.get("ssh_port", 22),
            ssh_key=hypervisor.get("ssh_key_path"),
            ssh_password=ssh_password,
        )

        try:
            result = inc_manager.setup_tracking(vmid)
            return result
        except Exception as e:
            logger.error(f"Error setting up incremental tracking for VM {vmid}: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to set up incremental tracking: {e}"
            )

    @app.delete("/api/v1/hypervisors/{hypervisor_id}/incremental-cleanup/{vmid}")
    def cleanup_vm_incremental_tracking(
        hypervisor_id: str,
        vmid: int,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Remove incremental backup tracking for a VM.

        Removes dirty bitmaps and clears tracking state from the database.
        """
        from backer.hypervisors.qmp import QMPClient, QMPError

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        # Get SSH credentials
        ssh_password = None
        if hypervisor.get("ssh_use_api_password", True):
            ssh_password = storage.get_hypervisor_password(hypervisor_id)

        # Try to clean up bitmaps on the VM
        bitmaps_removed = 0
        try:
            client = QMPClient(
                host=hypervisor["host"],
                vmid=vmid,
                ssh_user=hypervisor.get("ssh_user", "root"),
                ssh_port=hypervisor.get("ssh_port", 22),
                ssh_key=hypervisor.get("ssh_key_path"),
                ssh_password=ssh_password,
            )

            if client.is_vm_running():
                bitmaps_removed = client.cleanup_bitmaps()

        except QMPError as e:
            logger.warning(f"Could not clean up bitmaps on VM {vmid}: {e}")

        # Clean up database state
        db_removed = storage.delete_vm_bitmap_state(hypervisor_id, vmid)

        return {
            "vmid": vmid,
            "bitmaps_removed": bitmaps_removed,
            "db_records_removed": db_removed,
        }

    # ============ Hypervisor Backups ============

    @app.get("/api/v1/hypervisors/{hypervisor_id}/backups")
    def list_hypervisor_backups(
        hypervisor_id: str,
        node: str | None = None,
        storage_id: str | None = None,
        vmid: int | None = None,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List backups on a hypervisor storage.

        If node and storage_id are not provided, scans all backup-capable
        storages across all nodes.
        """
        from backer.hypervisors.proxmox import ProxmoxAPI, ProxmoxAuthMethod

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        token_secret = storage.get_hypervisor_token_secret(hypervisor_id)
        password = storage.get_hypervisor_password(hypervisor_id)

        auth_method = ProxmoxAuthMethod.TOKEN if hypervisor["auth_method"] == "token" else ProxmoxAuthMethod.PASSWORD

        api = ProxmoxAPI(
            host=hypervisor["host"],
            port=hypervisor["port"],
            token_id=hypervisor.get("token_id"),
            token_secret=token_secret,
            username=hypervisor.get("username"),
            password=password,
            auth_method=auth_method,
            verify_ssl=hypervisor.get("verify_ssl", False),
        )

        try:
            # Authenticate if using password-based auth
            if auth_method == ProxmoxAuthMethod.PASSWORD:
                api.authenticate()

            all_backups = []
            seen_volids: set[str] = set()  # Deduplicate for shared storage in clusters

            if node and storage_id:
                # Specific node and storage
                backups = api.list_backups(node, storage_id, vmid)
                for backup in backups:
                    if backup.volid not in seen_volids:
                        seen_volids.add(backup.volid)
                        all_backups.append(backup)
            else:
                # Scan all backup-capable storages across all nodes
                storages = api.list_storages()
                nodes = api.list_nodes()

                for pve_node in nodes:
                    for pve_storage in storages:
                        # Skip if storage not on this node
                        if pve_storage.node and pve_storage.node != pve_node.node:
                            continue
                        try:
                            backups = api.list_backups(pve_node.node, pve_storage.storage, vmid)
                            for backup in backups:
                                # Deduplicate - shared storage shows same backups on all nodes
                                if backup.volid not in seen_volids:
                                    seen_volids.add(backup.volid)
                                    all_backups.append(backup)
                        except Exception:
                            # Storage might not be accessible on this node
                            pass

            # Sort by ctime descending (newest first)
            all_backups.sort(key=lambda b: b.ctime, reverse=True)

            return [
                {
                    "volid": b.volid,
                    "vmid": b.vmid,
                    "node": b.node,
                    "ctime": b.ctime.timestamp(),
                    "size": b.size,
                    "format": b.format,
                    "notes": b.notes,
                    "protected": b.protected,
                    "guest_type": b.guest_type,  # qemu or lxc
                }
                for b in all_backups[:50]  # Limit to 50 most recent
            ]
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/v1/hypervisor-jobs/{job_id}/backups")
    def list_hypervisor_job_backups(
        job_id: str,
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List backups for a hypervisor job from repository metadata.

        This reads the backup metadata stored in the repository, which persists
        even if the Backer server is reinstalled. This is the source of truth
        for what backups exist in the repository.
        """
        import subprocess

        job = storage.get_hypervisor_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        repository_id = job.get("repository_id")
        if not repository_id:
            return []

        repository = storage.get_repository(repository_id)
        if not repository:
            return []

        hypervisor = storage.get_hypervisor(job["hypervisor_id"])
        if not hypervisor:
            return []

        repo_type = repository.get("repo_type", "").lower()
        server = repository.get("server", "")
        share = repository.get("share", "")
        subdir = repository.get("path", "")
        username = repository.get("username")
        domain = repository.get("domain")

        # Get repository password
        repo_password = storage.get_repository_password(repository_id)

        if repo_type != "smb" or not server or not share:
            # For non-SMB repos, fall back to local storage runs
            # TODO: Support NFS and local repos
            return _get_job_backups_from_local_storage(storage, job_id)

        # Build the path to metadata: {repo_path}/Hypervisors/{hypervisor_name}/.backer/
        base_path = subdir.strip("/") if subdir else ""
        safe_hv_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in hypervisor["name"])
        if base_path:
            remote_base = f"{base_path}/Hypervisors/{safe_hv_name}"
        else:
            remote_base = f"Hypervisors/{safe_hv_name}"

        # Build smbclient auth
        auth_parts = []
        if username:
            auth_parts.extend(["-U", f"{domain}\\{username}%{repo_password or ''}" if domain else f"{username}%{repo_password or ''}"])
        else:
            auth_parts.extend(["-N"])

        # List vzdump files in dump/ directory
        dump_path = f"{remote_base}/dump"
        logger.debug(f"Listing backups from //{server}/{share}/{dump_path}")

        list_cmd = [
            "smbclient", f"//{server}/{share}", *auth_parts,
            "-c", f"ls {dump_path}/vzdump-*"
        ]

        try:
            result = subprocess.run(list_cmd, capture_output=True, timeout=30, text=True)
            if result.returncode != 0:
                logger.debug(f"smbclient ls dump/ failed (trying without dump/): {result.stderr}")
                # Try without dump/ prefix (older structure)
                list_cmd[-1] = f"ls {remote_base}/vzdump-*"
                result = subprocess.run(list_cmd, capture_output=True, timeout=30, text=True)

            backups = []
            if result.returncode == 0 and result.stdout:
                # Parse smbclient ls output
                # Format: "  vzdump-qemu-100-2025_12_06-10_30_00.vma.zst      A   123456789  Fri Dec  6 10:35:00 2025"
                # Or:     "  vzdump-qemu-100-2025_12_06-10_30_00.vma.zst  N  123456789  Fri Dec  6 10:35:00 2025"
                import re
                for line in result.stdout.split("\n"):
                    # Skip empty lines and summary lines
                    if not line.strip() or "blocks of size" in line or "blocks available" in line:
                        continue

                    # Extract vzdump filename from line
                    # smbclient format: "  filename  ATTR  SIZE  DATE"
                    vzdump_match = re.search(
                        r"(vzdump-(qemu|lxc)-(\d+)-(\d{4}_\d{2}_\d{2})-(\d{2}_\d{2}_\d{2})\.vma(?:\.(zst|gz|lzo))?)",
                        line
                    )
                    if vzdump_match:
                        filename = vzdump_match.group(1)
                        guest_type = vzdump_match.group(2)
                        vmid = vzdump_match.group(3)
                        date_str = vzdump_match.group(4)
                        time_str = vzdump_match.group(5)
                        compression = vzdump_match.group(6)

                        # Try to extract size from the line (digits followed by date)
                        size_match = re.search(r"\s(\d+)\s+\w{3}\s+\w{3}", line)
                        size = int(size_match.group(1)) if size_match else 0

                        try:
                            backup_time = datetime.strptime(f"{date_str}_{time_str}", "%Y_%m_%d_%H_%M_%S")
                            backups.append({
                                "filename": filename,
                                "volid": f"backer:{dump_path}/{filename}",
                                "vmid": int(vmid),
                                "guest_type": guest_type,
                                "ctime": backup_time.timestamp(),
                                "size": size,
                                "format": "vma",
                                "node": hypervisor.get("name", "unknown"),
                                "compression": compression or "none",
                            })
                        except ValueError as e:
                            logger.debug(f"Failed to parse backup date from {filename}: {e}")

            # Sort by time descending
            backups.sort(key=lambda x: x.get("ctime", 0), reverse=True)
            return backups[:50]

        except subprocess.TimeoutExpired:
            logger.warning("Timeout listing backups from SMB share")
            return _get_job_backups_from_local_storage(storage, job_id)
        except Exception as e:
            logger.warning(f"Error listing backups from SMB: {e}")
            return _get_job_backups_from_local_storage(storage, job_id)

    def _get_job_backups_from_local_storage(storage: Storage, job_id: str) -> list[dict[str, Any]]:
        """Get backup history from local database for a hypervisor job."""
        runs = storage.get_hypervisor_runs(job_id=job_id, limit=100)
        backups = []
        for run in runs:
            if run.get("status") == "success":
                # Convert started_at to timestamp
                started_at = run.get("started_at")
                if isinstance(started_at, str):
                    try:
                        started_at = datetime.fromisoformat(started_at).timestamp()
                    except ValueError:
                        started_at = 0
                elif isinstance(started_at, datetime):
                    started_at = started_at.timestamp()
                else:
                    started_at = 0

                backups.append({
                    "filename": f"vzdump-qemu-{run.get('guest_id', 0)}-backup",
                    "volid": run.get("upid", ""),
                    "vmid": run.get("guest_id", 0),
                    "guest_type": "qemu",  # Default
                    "ctime": started_at,
                    "size": 0,  # Not tracked in run table
                    "format": "vma",
                    "node": run.get("guest_name", "unknown"),
                })
        return backups

    @app.post("/api/v1/hypervisors/{hypervisor_id}/restore")
    async def restore_hypervisor_backup(
        hypervisor_id: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Restore a VM/container from backup."""
        from backer.hypervisors.proxmox import (
            ProxmoxAPI,
            ProxmoxAuthMethod,
            ProxmoxBackupManager,
            ProxmoxGuestType,
        )

        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        body = await request.json()

        archive = body.get("archive")
        node = body.get("node")
        guest_type = body.get("guest_type", "qemu")
        target_vmid = body.get("vmid")  # Optional: restore to different VMID
        target_storage = body.get("storage")
        force = body.get("force", False)
        start_after = body.get("start", False)

        if not archive or not node:
            raise HTTPException(status_code=400, detail="archive and node are required")

        # Parse original VMID from backup filename (e.g., vzdump-qemu-100-2024...)
        import re
        vmid_match = re.search(r"vzdump-(?:qemu|lxc)-(\d+)-", archive)
        if not vmid_match:
            raise HTTPException(status_code=400, detail="Could not parse VMID from backup filename")
        original_vmid = int(vmid_match.group(1))

        # Use target_vmid if provided, otherwise restore to original VMID
        vmid = target_vmid or original_vmid

        token_secret = storage.get_hypervisor_token_secret(hypervisor_id)
        password = storage.get_hypervisor_password(hypervisor_id)

        auth_method = ProxmoxAuthMethod.TOKEN if hypervisor["auth_method"] == "token" else ProxmoxAuthMethod.PASSWORD

        api = ProxmoxAPI(
            host=hypervisor["host"],
            port=hypervisor["port"],
            token_id=hypervisor.get("token_id"),
            token_secret=token_secret,
            username=hypervisor.get("username"),
            password=password,
            auth_method=auth_method,
            verify_ssl=hypervisor.get("verify_ssl", False),
        )

        # Authenticate if using password-based auth
        if auth_method == ProxmoxAuthMethod.PASSWORD:
            api.authenticate()

        # Submit restore as background task
        def run_restore_task(task: Task) -> dict[str, Any]:
            manager = ProxmoxBackupManager(api)
            task.message = f"Restoring to VMID {vmid} from backup..."

            try:
                result = manager.restore_guest(
                    vmid=original_vmid,  # Original VMID from backup
                    archive=archive,
                    node=node,
                    guest_type=ProxmoxGuestType.QEMU if guest_type == "qemu" else ProxmoxGuestType.LXC,
                    target_vmid=vmid,  # Target VMID (may be same or different)
                    storage=target_storage,
                    force=force,
                    start_after=start_after,
                )

                task.progress = 100
                task.message = "Restore completed" if result["success"] else "Restore failed"
                return result

            except Exception as e:
                return {"success": False, "error": str(e)}

        task_manager = get_task_manager()
        task = task_manager.submit(
            task_type="hypervisor_restore",
            description=f"Restoring VMID {vmid} on {hypervisor['name']}",
            func=run_restore_task,
        )

        return {
            "task_id": task.id,
            "message": f"Restore started for VMID {vmid}",
        }

    @app.post("/api/v1/hypervisor-jobs/{job_id}/restore")
    async def restore_from_repository(
        job_id: str,
        request: Request,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Restore a VM/container from a backup stored in the Backer repository.

        This endpoint:
        1. Temporarily mounts the repository as Proxmox storage
        2. Performs the restore via Proxmox API
        3. Cleans up the temporary storage mount
        """
        from backer.hypervisors.proxmox import (
            ProxmoxAPI,
            ProxmoxAPIError,
            ProxmoxAuthMethod,
            ProxmoxBackupManager,
            ProxmoxGuestType,
        )

        job = storage.get_hypervisor_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        hypervisor_id = job["hypervisor_id"]
        hypervisor = storage.get_hypervisor(hypervisor_id)
        if not hypervisor:
            raise HTTPException(status_code=404, detail="Hypervisor not found")

        repository_id = job["repository_id"]
        repository = storage.get_repository(repository_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        body = await request.json()

        filename = body.get("filename")  # e.g., "vzdump-qemu-100-2025_12_07-10_04_54.vma.zst"
        target_vmid = body.get("vmid")  # Optional: restore to different VMID
        target_storage = body.get("storage")  # Optional: storage for VM disks
        force = body.get("force", False)
        start_after = body.get("start", False)
        guest_type = body.get("guest_type", "qemu")

        if not filename:
            raise HTTPException(status_code=400, detail="filename is required")

        # Parse original VMID from backup filename
        import re
        vmid_match = re.search(r"vzdump-(?:qemu|lxc)-(\d+)-", filename)
        if not vmid_match:
            raise HTTPException(status_code=400, detail="Could not parse VMID from backup filename")
        original_vmid = int(vmid_match.group(1))

        # Detect guest type from filename if not specified
        if "vzdump-lxc-" in filename:
            guest_type = "lxc"

        # Use target_vmid if provided, otherwise restore to original VMID
        vmid = target_vmid or original_vmid

        # Get credentials
        token_secret = storage.get_hypervisor_token_secret(hypervisor_id)
        hv_password = storage.get_hypervisor_password(hypervisor_id)
        repo_password = None
        if repository.get("repo_type") == "smb" and repository.get("has_password"):
            repo_password = storage.get_repository_password(repository_id)

        auth_method = ProxmoxAuthMethod.TOKEN if hypervisor["auth_method"] == "token" else ProxmoxAuthMethod.PASSWORD

        api = ProxmoxAPI(
            host=hypervisor["host"],
            port=hypervisor["port"],
            token_id=hypervisor.get("token_id"),
            token_secret=token_secret,
            username=hypervisor.get("username"),
            password=hv_password,
            auth_method=auth_method,
            verify_ssl=hypervisor.get("verify_ssl", False),
        )

        if auth_method == ProxmoxAuthMethod.PASSWORD:
            api.authenticate()

        # Get SSH credentials for storage cleanup
        ssh_user = job.get("ssh_user") or hypervisor.get("ssh_user", "root")
        ssh_port = job.get("ssh_port") or hypervisor.get("ssh_port", 22)
        ssh_key_path = hypervisor.get("ssh_key_path")
        ssh_password = None
        if hypervisor.get("ssh_use_api_password", True) and hv_password:
            ssh_password = hv_password

        # Build repository dict with password
        repo_with_password = {**repository, "password": repo_password}

        def run_restore_task(task: Task) -> dict[str, Any]:
            proxmox_storage_id = None
            try:
                # Step 1: Mount repository as Proxmox storage
                task.message = "Mounting backup repository..."
                task.progress = 10
                try:
                    proxmox_storage_id = api.ensure_backer_storage(
                        repo_with_password,
                        hypervisor_name=hypervisor["name"],
                        ssh_user=ssh_user,
                        ssh_port=ssh_port,
                        ssh_key=ssh_key_path,
                        ssh_password=ssh_password,
                    )
                except ProxmoxAPIError as e:
                    raise RuntimeError(f"Failed to mount repository: {e}") from e

                # Step 2: Find which node has the storage active
                task.message = "Finding target node..."
                task.progress = 20

                # Wait for storage to be active and get the node where it's mounted
                max_wait = 30
                poll_interval = 2
                waited = 0
                target_node = None

                while waited < max_wait:
                    is_active, active_node = api.is_storage_active_on_any_node(proxmox_storage_id)
                    if is_active and active_node:
                        target_node = active_node
                        logger.info(f"Storage '{proxmox_storage_id}' is active on node '{target_node}'")
                        break
                    logger.info(f"Waiting for storage to become active... ({waited}s)")
                    time.sleep(poll_interval)
                    waited += poll_interval

                if not target_node:
                    raise RuntimeError(
                        f"Storage '{proxmox_storage_id}' not active on any node after {max_wait}s. "
                        "Cannot perform restore."
                    )

                # Step 3: Build the archive volid
                # Format: "storage_id:backup/filename"
                archive = f"{proxmox_storage_id}:backup/{filename}"

                task.message = f"Restoring VMID {vmid} from {filename}..."
                task.progress = 30

                # Step 4: Perform restore
                manager = ProxmoxBackupManager(api)

                def progress_callback(status: Any, log_lines: list[str]) -> None:
                    # Update progress based on log lines
                    for line in log_lines:
                        if "extracting" in line.lower():
                            task.progress = min(task.progress + 5, 90)
                        if "INFO:" in line:
                            short_line = line.replace("INFO:", "").strip()[:60]
                            task.message = f"Restoring: {short_line}"

                result = manager.restore_guest(
                    vmid=original_vmid,
                    archive=archive,
                    node=target_node,
                    guest_type=ProxmoxGuestType.QEMU if guest_type == "qemu" else ProxmoxGuestType.LXC,
                    target_vmid=vmid,
                    storage=target_storage,
                    force=force,
                    start_after=start_after,
                    progress_callback=progress_callback,
                )

                task.progress = 95
                task.message = "Cleaning up..."

                return result

            except Exception as e:
                logger.exception(f"Restore failed: {e}")
                return {"success": False, "error": str(e)}

            finally:
                # Step 5: Clean up temporary storage mount
                if proxmox_storage_id:
                    try:
                        api.delete_storage(proxmox_storage_id)
                        logger.info(f"Cleaned up temporary Proxmox storage '{proxmox_storage_id}'")
                    except Exception as cleanup_err:
                        logger.warning(f"Failed to cleanup storage '{proxmox_storage_id}': {cleanup_err}")

        task_manager = get_task_manager()
        task = task_manager.submit(
            task_type="hypervisor_restore",
            description=f"Restoring VMID {vmid} from {filename}",
            func=run_restore_task,
        )

        return {
            "task_id": task.id,
            "message": f"Restore started for VMID {vmid}",
            "filename": filename,
        }

    # ============ Logs API ============

    @app.get("/api/v1/logs")
    def list_log_files(
        storage: Storage = Depends(get_storage),
    ) -> list[dict[str, Any]]:
        """List available log files."""
        log_dir = data_dir / "logs"
        if not log_dir.exists():
            return []

        log_files = []
        for log_file in sorted(log_dir.glob("*.log"), reverse=True):
            stat = log_file.stat()
            log_files.append({
                "name": log_file.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })

        return log_files

    @app.get("/api/v1/logs/{filename}")
    def get_log_content(
        filename: str,
        lines: int = 500,
        offset: int = 0,
        level: str | None = None,
        search: str | None = None,
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Get content of a log file.

        Args:
            filename: Log file name
            lines: Number of lines to return (default 500)
            offset: Number of lines to skip from end (for pagination)
            level: Filter by log level (DEBUG, INFO, WARNING, ERROR)
            search: Search string to filter logs
        """
        import re

        # Validate filename to prevent path traversal
        if ".." in filename or "/" in filename or "\\" in filename:
            raise HTTPException(status_code=400, detail="Invalid filename")

        log_dir = data_dir / "logs"
        log_file = log_dir / filename

        if not log_file.exists() or not log_file.is_file():
            raise HTTPException(status_code=404, detail="Log file not found")

        # Read the file
        try:
            with open(log_file, encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to read log file: {e}")

        total_lines = len(all_lines)

        # Filter by log level if specified
        if level:
            level_upper = level.upper()
            all_lines = [line for line in all_lines if f" - {level_upper} - " in line]

        # Filter by search term if specified
        if search:
            search_lower = search.lower()
            all_lines = [line for line in all_lines if search_lower in line.lower()]

        filtered_total = len(all_lines)

        # Get the requested range (from end, newest first)
        if offset > 0:
            end_idx = len(all_lines) - offset
            start_idx = max(0, end_idx - lines)
            selected_lines = all_lines[start_idx:end_idx]
        else:
            # Get last N lines
            selected_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines

        # Reverse to show newest first
        selected_lines = list(reversed(selected_lines))

        # Parse log lines for structured output
        log_entries = []
        log_pattern = re.compile(
            r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - ([^-]+) - (\w+) - (.*)$'
        )

        for line in selected_lines:
            line = line.rstrip('\n\r')
            match = log_pattern.match(line)
            if match:
                log_entries.append({
                    "timestamp": match.group(1),
                    "logger": match.group(2).strip(),
                    "level": match.group(3),
                    "message": match.group(4),
                    "raw": line,
                })
            else:
                # Non-matching line (continuation or different format)
                log_entries.append({
                    "timestamp": "",
                    "logger": "",
                    "level": "",
                    "message": line,
                    "raw": line,
                })

        return {
            "filename": filename,
            "total_lines": total_lines,
            "filtered_lines": filtered_total,
            "returned_lines": len(log_entries),
            "offset": offset,
            "entries": log_entries,
        }

    @app.get("/api/v1/logs/stream/{filename}")
    async def stream_log(
        filename: str,
        storage: Storage = Depends(get_storage),
    ):
        """Stream log file updates using Server-Sent Events."""
        import asyncio

        from fastapi.responses import StreamingResponse

        # Validate filename to prevent path traversal
        if ".." in filename or "/" in filename or "\\" in filename:
            raise HTTPException(status_code=400, detail="Invalid filename")

        log_dir = data_dir / "logs"
        log_file = log_dir / filename

        if not log_file.exists() or not log_file.is_file():
            raise HTTPException(status_code=404, detail="Log file not found")

        async def generate():
            """Generate SSE events for new log lines."""
            try:
                with open(log_file, encoding="utf-8", errors="replace") as f:
                    # Seek to end of file
                    f.seek(0, 2)

                    while True:
                        line = f.readline()
                        if line:
                            # Send as SSE event
                            yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"
                        else:
                            # No new data, wait a bit
                            await asyncio.sleep(0.5)
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )

    # ============ Web UI ============

    # Store storage in app state for web routes
    app.state.storage = _storage

    # Mount static files
    static_dir = Path(__file__).parent / "web" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Add web authentication middleware
    from backer.server.web.auth import AuthMiddleware, ensure_default_user
    app.add_middleware(AuthMiddleware)

    # Ensure default admin user exists
    ensure_default_user(_storage)

    # Include web UI routes
    app.include_router(web_router)

    return app
