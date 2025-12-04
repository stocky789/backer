"""FastAPI application for the Backer server."""

import hashlib
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
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
from backer.server.web.routes import router as web_router

logger = logging.getLogger(__name__)

# Global storage instance (initialized in create_app)
_storage: Storage | None = None
_scheduler: BackupScheduler | None = None

security = HTTPBasic(auto_error=False)


def get_storage() -> Storage:
    """Get the storage instance."""
    if _storage is None:
        raise RuntimeError("Storage not initialized")
    return _storage


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

    # Queue the backup command for the client
    command_payload = {
        "job_name": job_name,
        "run_id": run_id,
        "source_path": job.get("source_path"),
        "destination_path": job.get("destination_path"),
        "backend": job.get("backend", "rclone"),
        "excludes": job.get("excludes", []),
        "backend_options": job.get("backend_options", {}),  # Includes restic_password
        "dry_run": False,
    }

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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Health check
    @app.get("/health")
    def health_check() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

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
    def delete_client(client_id: str, storage: Storage = Depends(get_storage)) -> dict[str, str]:
        """Remove a client."""
        if not storage.delete_client(client_id):
            raise HTTPException(status_code=404, detail="Client not found")
        return {"status": "deleted"}

    @app.post("/api/v1/clients/heartbeat")
    def client_heartbeat(
        heartbeat: ClientHeartbeat,
        req: Request,
        client: Client = Depends(verify_client),
        storage: Storage = Depends(get_storage),
    ) -> dict[str, Any]:
        """Receive heartbeat from a client."""
        storage.update_client_status(
            client.id, ClientStatus.ONLINE, ip_address=req.client.host if req.client else None
        )
        # Get pending commands for this client
        commands = storage.get_pending_commands(client.id)
        if commands:
            logger.info(f"[HEARTBEAT] Agent '{client.id}' receiving {len(commands)} command(s)")
            for cmd in commands:
                logger.debug(f"[HEARTBEAT] Command: {cmd}")
        return {"status": "ok", "commands": commands}

    # ============ Job Management ============

    @app.post("/api/v1/jobs", response_model=JobResponse)
    def create_job(job: JobCreate, storage: Storage = Depends(get_storage)) -> JobResponse:
        """Create a new backup job."""
        # Validate job name
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
    def delete_job(job_name: str, storage: Storage = Depends(get_storage)) -> dict[str, str]:
        """Delete a job."""
        if not storage.delete_job(job_name):
            raise HTTPException(status_code=404, detail="Job not found")
        return {"status": "deleted"}

    @app.post("/api/v1/jobs/{job_name}/run", response_model=JobRunResponse)
    def run_job(
        job_name: str,
        request: JobRunRequest,
        storage: Storage = Depends(get_storage),
    ) -> JobRunResponse:
        """Trigger a job to run."""
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

        # Queue the backup command for the client
        command_payload = {
            "job_name": job_name,
            "run_id": run_id,
            "source_path": job.get("source_path"),
            "destination_path": job.get("destination_path"),
            "backend": job.get("backend", "rclone"),
            "excludes": job.get("excludes", []),
            "backend_options": job.get("backend_options", {}),  # Includes restic_password
            "dry_run": request.dry_run,
        }

        storage.queue_command(
            client_id=client_id,
            command_type="backup",
            payload=command_payload,
        )

        logger.info(f"[JOB RUN] Job '{job_name}' queued for agent '{client_id}' (run_id: {run_id})")
        logger.debug(f"[JOB RUN] Command payload: {command_payload}")

        return JobRunResponse(
            run_id=run_id,
            job_name=job_name,
            status="pending",
            started_at=started_at,
            message=f"Backup queued for agent '{client_id}'" + (" (dry run)" if request.dry_run else ""),
        )

    @app.get("/api/v1/jobs/{job_name}/runs")
    def get_job_runs(
        job_name: str, limit: int = 20, storage: Storage = Depends(get_storage)
    ) -> list[dict[str, Any]]:
        """Get run history for a job."""
        if not storage.get_job(job_name):
            raise HTTPException(status_code=404, detail="Job not found")
        return storage.get_job_runs(job_name, limit)

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
        command_payload = {
            "job_name": job_name,
            "run_id": f"restore_{restore_id}",
            "source_path": backup_source,  # Restore FROM backup location
            "destination_path": destination_path,  # Restore TO this location
            "backend": backend,
            "backend_options": job.get("backend_options", {}),  # Includes restic_password
            "snapshot": data.get("snapshot"),
            "source_subfolder": source_subfolder if backend == "restic" else "",  # For restic --include
            "clean_restore": data.get("clean_restore", False),  # Delete extra files at destination
            "dry_run": data.get("dry_run", False),
        }

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

        For SMB/NFS repositories, this will temporarily mount the share if needed.
        """
        from backer.core.repo_metadata import RepositoryMetadata
        from backer.server.repositories import temporary_mount

        repo = storage.get_repository(repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found")

        repo_type = repo.get("repo_type", "smb")
        subpath = repo.get("path", "")

        # Determine how to access the repository
        if repo.get("mount_point"):
            # Use existing mount point
            repo_path = repo["mount_point"]
            if subpath:
                repo_path = repo_path.rstrip("/") + "/" + subpath
            use_temp_mount = False
        elif repo_type == "local":
            # Local repository - use share or path field
            repo_path = repo.get("share", "") or repo.get("path", "")
            use_temp_mount = False
        else:
            # Need to temporarily mount SMB/NFS share
            use_temp_mount = True
            repo_path = None

        def do_scan(scan_path: str) -> dict[str, Any]:
            """Perform the actual scan."""
            repo_meta = RepositoryMetadata(scan_path, repo_type)
            discovery = repo_meta.discover_all()

            return {
                "success": True,
                "repository_id": repo_id,
                "repository_name": repo.get("name"),
                "path": scan_path,
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
            if use_temp_mount:
                # Get credentials for mounting
                password = storage.get_repository_password(repo_id)

                with temporary_mount(
                    repo_type=repo_type,
                    server=repo.get("server", ""),
                    share=repo.get("share", ""),
                    username=repo.get("username"),
                    password=password,
                    domain=repo.get("domain"),
                ) as (success, mount_result):
                    if not success:
                        return {
                            "success": False,
                            "error": mount_result,
                            "repository_id": repo_id,
                            "repository_name": repo.get("name"),
                            "hint": "Ensure the server is running as root or has mount permissions",
                        }

                    # Build full path with subpath
                    scan_path = mount_result
                    if subpath:
                        scan_path = mount_result.rstrip("/") + "/" + subpath

                    return do_scan(scan_path)
            else:
                return do_scan(repo_path)

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
        """
        from backer.server.repo_metadata import import_repository_metadata

        repo = storage.get_repository(repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found")

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
            result = import_repository_metadata(repo_path, storage, repo_id)
            return {
                "repository_id": repo_id,
                "repository_name": repo.get("name"),
                **result,
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
