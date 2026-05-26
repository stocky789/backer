"""Web UI routes for Backer server."""

from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from backer import __version__
from backer.server import timezone as tz
from backer.server.storage import Storage
from backer.server.web.auth import (
    clear_session_cookie,
    create_session,
    get_current_user,
    hash_password,
    set_session_cookie,
    verify_password,
)

router = APIRouter()

# Templates directory
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Add version to global template context so it's available in base.html
templates.env.globals["backer_version"] = __version__


def render_template(request: Request, name: str, context: dict[str, Any]):
    """Render a Jinja template using Starlette's current request-first API."""
    context.setdefault("request", request)
    return templates.TemplateResponse(request, name, context)


def get_storage(request: Request) -> Storage:
    """Get storage from app state."""
    return request.app.state.storage


def get_timezone(storage: Storage) -> tzinfo:
    """Get configured timezone from storage."""
    tz_name = storage.get_setting("timezone", "UTC")
    try:
        return UTC if not tz_name or tz_name.upper() == "UTC" else ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return UTC
    except Exception:
        return UTC


def time_ago(dt: datetime | str | None) -> str:
    """Format datetime as 'X ago' string."""
    if dt is None:
        return "Never"

    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return dt

    now = datetime.now()
    if dt.tzinfo:
        now = datetime.now(dt.tzinfo)

    diff = now - dt

    if diff.days > 30:
        return dt.strftime("%Y-%m-%d")
    elif diff.days > 0:
        return f"{diff.days}d ago"
    elif diff.seconds > 3600:
        return f"{diff.seconds // 3600}h ago"
    elif diff.seconds > 60:
        return f"{diff.seconds // 60}m ago"
    else:
        return "Just now"


def format_bytes(bytes_val: int) -> str:
    """Format bytes as human readable."""
    if bytes_val == 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_val < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} PB"


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Dashboard page."""
    storage = get_storage(request)

    # Get agents
    agents_raw = storage.list_clients()
    agents = []
    online_count = 0

    for a in agents_raw:
        is_online = False
        if a.last_seen:
            # Consider online if seen in last 2 minutes
            diff = tz.get_now() - a.last_seen
            is_online = diff.total_seconds() < 120

        if is_online:
            online_count += 1

        agents.append({
            **a.__dict__,
            "status": "online" if is_online else "offline",
            "last_seen_ago": time_ago(a.last_seen),
        })

    # Get jobs
    jobs_raw = storage.list_jobs()
    jobs = []
    for j in jobs_raw:
        latest = storage.get_latest_run(j["name"])
        jobs.append({
            **j,
            "last_status": latest["status"] if latest else None,
            "last_run": latest["started_at"] if latest else None,
        })

    # Get recent runs (agent backups)
    recent_runs = []
    for job in jobs_raw[:10]:
        runs = storage.get_job_runs(job["name"], limit=5)
        for run in runs:
            recent_runs.append({
                **run,
                "run_type": "agent",
                "started_at_ago": time_ago(run.get("started_at")),
            })

    # Get hypervisors and hypervisor jobs
    hypervisors_raw = storage.list_hypervisors()
    hypervisors = []
    connected_hv_count = 0

    for hv in hypervisors_raw:
        is_connected = hv.get("status") == "connected"
        if is_connected:
            connected_hv_count += 1
        hypervisors.append({
            **hv,
            "last_checked_ago": time_ago(hv.get("last_checked")),
        })

    # Get hypervisor jobs
    hv_jobs_raw = storage.list_hypervisor_jobs()
    hv_jobs = []
    for job in hv_jobs_raw:
        latest = storage.get_latest_hypervisor_run(job["id"])
        hv = storage.get_hypervisor(job["hypervisor_id"])
        guest_ids = job.get("guest_ids") or []
        hv_jobs.append({
            **job,
            "hypervisor_name": hv.get("name") if hv else None,
            "guest_count": len(guest_ids) if guest_ids else 0,
            "last_status": latest["status"] if latest else None,
            "last_run_ago": time_ago(latest.get("started_at")) if latest else None,
        })

    # Get recent hypervisor runs
    hv_runs = storage.get_hypervisor_runs(limit=10)
    for run in hv_runs:
        # Get job name
        job = storage.get_hypervisor_job(run.get("job_id"))
        recent_runs.append({
            **run,
            "run_type": "hypervisor",
            "job_name": job.get("name") if job else f"VM {run.get('guest_id')}",
            "started_at_ago": time_ago(run.get("started_at")),
        })

    # Sort by time and take top 10
    recent_runs.sort(key=lambda x: x.get("started_at", ""), reverse=True)
    recent_runs = recent_runs[:10]

    # Get repositories with storage stats
    repositories = storage.list_repositories()
    repos_with_stats = []

    for repo in repositories:
        repo_stats = repo.copy()
        # Storage stats will be fetched via API call from frontend for performance
        repo_stats["used_bytes"] = None
        repo_stats["total_bytes"] = None
        repo_stats["used_percent"] = None
        repos_with_stats.append(repo_stats)

    # Stats
    today = datetime.now().strftime("%Y-%m-%d")
    backups_today = sum(1 for r in recent_runs if r.get("started_at", "")[:10] == today)

    stats = {
        "agents_online": online_count,
        "total_agents": len(agents),
        "total_jobs": len(jobs),
        "total_repositories": len(repositories),
        "backups_today": backups_today,
        # Hypervisor stats
        "hypervisors_connected": connected_hv_count,
        "total_hypervisors": len(hypervisors),
        "total_hv_jobs": len(hv_jobs),
    }

    return render_template(request, "dashboard.html", {
        "request": request,
        "active": "dashboard",
        "stats": stats,
        "agents": agents,
        "jobs": jobs,
        "recent_runs": recent_runs,
        "hypervisors": hypervisors,
        "hv_jobs": hv_jobs,
        "repositories": repos_with_stats,
        "user": get_current_user(request),
    })


@router.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request):
    """Agents management page."""
    storage = get_storage(request)

    agents_raw = storage.list_clients()
    agents = []

    for a in agents_raw:
        is_online = False
        if a.last_seen:
            diff = tz.get_now() - a.last_seen
            is_online = diff.total_seconds() < 120

        agents.append({
            **a.__dict__,
            "status": "online" if is_online else "offline",
            "last_seen_ago": time_ago(a.last_seen),
        })

    return render_template(request, "agents.html", {
        "request": request,
        "active": "agents",
        "agents": agents,
        "user": get_current_user(request),
    })


@router.get("/hypervisors", response_class=HTMLResponse)
async def hypervisors_page(request: Request):
    """Hypervisors management page."""
    storage = get_storage(request)

    # Get hypervisors
    hypervisors_raw = storage.list_hypervisors()
    hypervisors = []

    for hv in hypervisors_raw:
        hypervisors.append({
            **hv,
            "last_checked_ago": time_ago(hv.get("last_checked")),
        })

    # Get hypervisor jobs with additional info
    jobs_raw = storage.list_hypervisor_jobs()
    jobs = []

    for job in jobs_raw:
        # Get latest run
        latest = storage.get_latest_hypervisor_run(job["id"])

        # Count selected guests
        guest_ids = job.get("guest_ids") or []
        guest_count = len(guest_ids) if guest_ids else 0

        # Get hypervisor name
        hv = storage.get_hypervisor(job["hypervisor_id"])
        hv_name = hv.get("name") if hv else None

        jobs.append({
            **job,
            "hypervisor_name": hv_name,
            "guest_count": guest_count,
            "last_status": latest["status"] if latest else None,
            "last_run_ago": time_ago(latest.get("started_at")) if latest else None,
        })

    return render_template(request, "hypervisors.html", {
        "request": request,
        "active": "hypervisors",
        "hypervisors": hypervisors,
        "jobs": jobs,
        "user": get_current_user(request),
    })


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request):
    """Jobs management page."""
    storage = get_storage(request)

    jobs_raw = storage.list_jobs()
    jobs = []

    for j in jobs_raw:
        latest = storage.get_latest_run(j["name"])

        # Check if there are any successful backups (for restore button)
        runs = storage.get_job_runs(j["name"], limit=50)
        has_backups = any(
            r.get("status") == "success" and not r.get("run_id", "").startswith("restore_")
            for r in runs
        )

        # Calculate next run if scheduler available (using configured timezone)
        next_run = None
        schedule = j.get("schedule_cron")
        if schedule and j.get("enabled", True):
            try:
                from croniter import croniter
                tz = get_timezone(storage)
                cron = croniter(schedule, datetime.now(tz))
                next_run = cron.get_next(datetime)
            except Exception:
                pass

        jobs.append({
            **j,
            "last_status": latest["status"] if latest else None,
            "next_run": next_run,
            "has_backups": has_backups,
        })

    # Get agents for edit modal
    agents = storage.list_clients()

    return render_template(request, "jobs.html", {
        "request": request,
        "active": "jobs",
        "jobs": jobs,
        "agents": [{"id": a.id, "name": a.name, "hostname": a.hostname} for a in agents],
        "user": get_current_user(request),
    })


@router.get("/jobs/new", response_class=HTMLResponse)
async def jobs_new_page(request: Request):
    """New job form page."""
    storage = get_storage(request)
    agents = storage.list_clients()
    repositories = storage.list_repositories()

    return render_template(request, "jobs_new.html", {
        "request": request,
        "active": "jobs",
        "agents": [{"id": a.id, "name": a.name, "hostname": a.hostname} for a in agents],
        "repositories": repositories,
        "user": get_current_user(request),
    })


@router.post("/jobs/create")
async def jobs_create(
    request: Request,
    name: str = Form(...),
    source_path: str = Form(...),
    repository_id: str = Form(...),
    dest_subfolder: str = Form(""),
    backend: str = Form("rclone"),
    client_id: str = Form(...),
    excludes: str = Form(""),
    schedule_cron: str = Form(""),
    schedule_preset: str = Form(""),
    retention_preset: str = Form("standard"),
    retention_keep_last: int = Form(None),
    retention_keep_daily: int = Form(None),
    retention_keep_weekly: int = Form(None),
    retention_keep_monthly: int = Form(None),
    restic_password: str = Form(""),
):
    """Create a new backup job."""
    storage = get_storage(request)

    # Validate job name
    name = name.strip()
    if name.lower().startswith("restore:"):
        return RedirectResponse("/jobs/new?error=invalid_name", status_code=303)

    # Check for existing job with same name
    if storage.get_job(name):
        return RedirectResponse("/jobs/new?error=job_exists", status_code=303)

    # Build destination path from repository
    repo = storage.get_repository(repository_id)
    if not repo:
        return RedirectResponse("/jobs/new?error=invalid_repo", status_code=303)

    # Build full destination path based on repository type
    repo_type = repo.get("repo_type", "smb")
    if repo_type == "smb":
        destination_path = f"//{repo['server']}/{repo['share']}"
        if repo.get("path"):
            destination_path += "/" + repo["path"]
    elif repo_type == "nfs":
        destination_path = f"{repo['server']}:{repo['share']}"
        if repo.get("path"):
            destination_path += "/" + repo["path"]
    elif repo_type == "s3":
        destination_path = f"s3://{repo['share']}"
        if repo.get("path"):
            destination_path += "/" + repo["path"]
    elif repo_type == "local":
        destination_path = repo.get("share", "") or repo.get("path", "")
    else:
        # Fallback for unknown types
        destination_path = repo.get("share", "") or repo.get("path", "")

    if dest_subfolder:
        destination_path += "/" + dest_subfolder.strip("/")

    # Handle schedule - use preset if custom cron not provided
    final_schedule = schedule_cron if schedule_cron else (schedule_preset if schedule_preset != "custom" else None)

    # Build retention policy
    retention = {}
    if retention_preset == "custom":
        if retention_keep_last:
            retention["keep_last"] = retention_keep_last
        if retention_keep_daily:
            retention["keep_daily"] = retention_keep_daily
        if retention_keep_weekly:
            retention["keep_weekly"] = retention_keep_weekly
        if retention_keep_monthly:
            retention["keep_monthly"] = retention_keep_monthly
    elif retention_preset == "minimal":
        retention = {"keep_last": 3}
    elif retention_preset == "standard":
        retention = {"keep_last": 7, "keep_daily": 7, "keep_weekly": 4}
    elif retention_preset == "extended":
        retention = {"keep_last": 14, "keep_daily": 14, "keep_weekly": 8, "keep_monthly": 12}
    # "forever" preset means no retention policy (keep all)

    # Build backend options (for restic/kopia password, etc.)
    backend_options = {}
    if backend in ("restic", "kopia") and restic_password:
        backend_options["restic_password"] = restic_password

    job_config = {
        "source_path": source_path,
        "destination_path": destination_path,
        "repository_id": repository_id,
        "backend": backend,
        "client_id": client_id if client_id else None,
        "excludes": [e.strip() for e in excludes.split(",") if e.strip()],
        "schedule_cron": final_schedule if final_schedule else None,
        "retention": retention,
        "backend_options": backend_options,
        "enabled": True,
    }

    storage.save_job(name, job_config)

    return RedirectResponse("/jobs", status_code=303)


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    """Backup history page."""
    storage = get_storage(request)

    try:
        # Get all runs (backups and restores) directly from the database
        # This is more efficient and includes restore jobs which have different job_names
        runs = storage.get_all_job_runs(limit=100)
        all_runs = []

        for run in runs:
            started = run.get("started_at")
            finished = run.get("finished_at")

            duration = None
            if started and finished:
                try:
                    start_dt = datetime.fromisoformat(started)
                    end_dt = datetime.fromisoformat(finished)
                    diff = (end_dt - start_dt).total_seconds()
                    if diff < 60:
                        duration = f"{diff:.0f}s"
                    else:
                        duration = f"{diff // 60:.0f}m {diff % 60:.0f}s"
                except (ValueError, AttributeError, TypeError):
                    pass

            formatted_time = (
                started[:19].replace("T", " ") if started and len(started) >= 19 else (started if started else "-")
            )
            all_runs.append({
                **run,
                "run_type": "agent",
                "started_at_formatted": formatted_time,
                "started_at_ago": time_ago(started),
                "duration": duration,
                "bytes_formatted": format_bytes(run.get("bytes_transferred", 0)),
            })

        # Also get hypervisor runs
        hv_runs = storage.get_hypervisor_runs(limit=100)
        for run in hv_runs:
            started = run.get("started_at")
            finished = run.get("finished_at")

            duration = None
            if run.get("duration_seconds"):
                diff = run["duration_seconds"]
                if diff < 60:
                    duration = f"{diff:.0f}s"
                else:
                    duration = f"{diff // 60:.0f}m {diff % 60:.0f}s"
            elif started and finished:
                try:
                    start_dt = datetime.fromisoformat(started)
                    end_dt = datetime.fromisoformat(finished)
                    diff = (end_dt - start_dt).total_seconds()
                    if diff < 60:
                        duration = f"{diff:.0f}s"
                    else:
                        duration = f"{diff // 60:.0f}m {diff % 60:.0f}s"
                except (ValueError, AttributeError, TypeError):
                    pass

            formatted_time = (
                started[:19].replace("T", " ") if started and len(started) >= 19 else (started if started else "-")
            )
            all_runs.append({
                **run,
                "run_type": "hypervisor",
                "started_at_formatted": formatted_time,
                "started_at_ago": time_ago(started),
                "duration": duration,
                "bytes_formatted": "-",  # Hypervisor runs don't track bytes the same way
            })

        # Sort all runs by started_at descending
        all_runs.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        all_runs = all_runs[:100]  # Limit to 100 total

    except Exception as e:
        # Log the error and return empty history
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error loading history page: {e}", exc_info=True)
        all_runs = []

    return render_template(request, "history.html", {
        "request": request,
        "active": "history",
        "runs": all_runs,
        "user": get_current_user(request),
    })


@router.get("/storage", response_class=HTMLResponse)
async def storage_page(request: Request):
    """Storage repositories page."""
    storage = get_storage(request)
    repositories = storage.list_repositories()

    return render_template(request, "repositories.html", {
        "request": request,
        "active": "storage",
        "repositories": repositories,
        "user": get_current_user(request),
    })


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    """Server logs page."""
    return render_template(request, "logs.html", {
        "request": request,
        "active": "logs",
        "user": get_current_user(request),
    })


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Settings page."""
    from backer import __version__

    # Get server URL from request
    server_url = f"{request.url.scheme}://{request.url.netloc}"

    # Get data directory from storage path
    storage = get_storage(request)
    data_dir = str(storage.db_path.parent)

    # Get current timezone setting
    timezone = storage.get_setting("timezone", "UTC")

    # Get public URL setting for reverse proxy support
    public_url = storage.get_setting("public_url", "http://localhost:8420")

    # Check if settings were just saved
    settings_saved = request.query_params.get("saved") == "1"

    return render_template(request, "settings.html", {
        "request": request,
        "active": "settings",
        "version": __version__,
        "server_url": server_url,
        "data_dir": data_dir,
        "timezone": timezone,
        "public_url": public_url,
        "settings_saved": settings_saved,
        "user": get_current_user(request),
    })


@router.post("/settings/save")
async def settings_save(
    request: Request,
    timezone: str = Form("UTC"),
    public_url: str = Form("http://localhost:8420"),
):
    """Save settings."""
    storage = get_storage(request)
    storage.set_setting("timezone", timezone)
    storage.set_setting("public_url", public_url)

    # Clear timezone cache so new setting takes effect immediately
    tz.clear_cache()

    return RedirectResponse("/settings?saved=1", status_code=303)


@router.get("/restore", response_class=HTMLResponse)
async def restore_page(request: Request):
    """Restore page."""
    storage = get_storage(request)

    # Get jobs (to select backup source)
    jobs = storage.list_jobs()

    # Get agents (to select restore target)
    agents = storage.list_clients()

    # Get recent restore operations
    all_runs = []
    for job in jobs:
        runs = storage.get_job_runs(f"restore:{job['name']}", limit=10)
        for run in runs:
            all_runs.append({
                **run,
                "started_at_ago": time_ago(run.get("started_at")),
            })

    # Sort by time
    all_runs.sort(key=lambda x: x.get("started_at", ""), reverse=True)
    recent_restores = all_runs[:10]

    return render_template(request, "restore.html", {
        "request": request,
        "active": "restore",
        "jobs": jobs,
        "agents": [{"id": a.id, "name": a.name, "hostname": a.hostname} for a in agents],
        "recent_restores": recent_restores,
        "user": get_current_user(request),
    })


# Authentication routes
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Login page."""
    # If already logged in, redirect to dashboard
    if get_current_user(request):
        return RedirectResponse(url="/", status_code=303)

    error = request.query_params.get("error")
    error_messages = {
        "invalid": "Invalid username or password",
        "required": "Please log in to continue",
    }

    return render_template(request, "login.html", {
        "request": request,
        "error": error_messages.get(error),
        "username": "",
    })


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """Handle login form submission."""
    storage = get_storage(request)

    # Look up user
    user = storage.get_user_by_username(username)
    if not user or not verify_password(password, user.get("password_hash", "")):
        return render_template(request, "login.html", {
            "request": request,
            "error": "Invalid username or password",
            "username": username,
        })

    # Create session
    token = create_session(
        user_id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
    )

    # Update last login
    storage.update_last_login(user["id"])

    # Redirect to dashboard with session cookie
    response = RedirectResponse(url="/", status_code=303)
    set_session_cookie(response, token)
    return response


@router.post("/logout")
async def logout(request: Request):
    """Handle logout."""
    from backer.server.web.auth import SESSION_COOKIE_NAME, destroy_session

    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        destroy_session(token)

    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response)
    return response


@router.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    """Profile page."""
    storage = get_storage(request)
    session_user = get_current_user(request)

    if not session_user:
        return RedirectResponse(url="/login?error=required", status_code=303)

    # Get full user data from database
    user = storage.get_user(session_user["user_id"])
    if not user:
        return RedirectResponse(url="/login?error=required", status_code=303)

    saved = request.query_params.get("saved") == "1"
    password_changed = request.query_params.get("password_changed") == "1"

    return render_template(request, "profile.html", {
        "request": request,
        "active": "profile",
        "user": user,
        "saved": saved,
        "password_changed": password_changed,
    })


@router.post("/profile")
async def profile_update(
    request: Request,
    action: str = Form(...),
    display_name: str = Form(None),
    email: str = Form(None),
    current_password: str = Form(None),
    new_password: str = Form(None),
    confirm_password: str = Form(None),
):
    """Handle profile updates."""
    storage = get_storage(request)
    session_user = get_current_user(request)

    if not session_user:
        return RedirectResponse(url="/login?error=required", status_code=303)

    user = storage.get_user(session_user["user_id"])
    if not user:
        return RedirectResponse(url="/login?error=required", status_code=303)

    if action == "update_profile":
        # Update display name and email
        storage.update_user(
            user_id=user["id"],
            display_name=display_name or user["display_name"],
            email=email if email else None,
        )
        return RedirectResponse(url="/profile?saved=1", status_code=303)

    elif action == "change_password":
        # Verify current password
        if not current_password or not verify_password(current_password, user.get("password_hash", "")):
            return render_template(request, "profile.html", {
                "request": request,
                "active": "profile",
                "user": user,
                "password_error": "Current password is incorrect",
            })

        # Check new passwords match
        if not new_password or new_password != confirm_password:
            return render_template(request, "profile.html", {
                "request": request,
                "active": "profile",
                "user": user,
                "password_error": "New passwords do not match",
            })

        # Check password length
        if len(new_password) < 4:
            return render_template(request, "profile.html", {
                "request": request,
                "active": "profile",
                "user": user,
                "password_error": "Password must be at least 4 characters",
            })

        # Update password
        new_hash = hash_password(new_password)
        storage.update_user(user_id=user["id"], password_hash=new_hash)

        return RedirectResponse(url="/profile?password_changed=1", status_code=303)

    return RedirectResponse(url="/profile", status_code=303)
