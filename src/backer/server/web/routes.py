"""Web UI routes for Backer server."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from backer.server.storage import Storage

router = APIRouter()

# Templates directory
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def get_storage(request: Request) -> Storage:
    """Get storage from app state."""
    return request.app.state.storage


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
            diff = datetime.now() - a.last_seen
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

    # Get recent runs
    recent_runs = []
    for job in jobs_raw[:10]:
        runs = storage.get_job_runs(job["name"], limit=5)
        for run in runs:
            recent_runs.append({
                **run,
                "started_at_ago": time_ago(run.get("started_at")),
            })

    # Sort by time and take top 10
    recent_runs.sort(key=lambda x: x.get("started_at", ""), reverse=True)
    recent_runs = recent_runs[:10]

    # Stats
    stats = {
        "agents_online": online_count,
        "total_agents": len(agents),
        "total_jobs": len(jobs),
        "backups_today": sum(1 for r in recent_runs if r.get("started_at", "")[:10] == datetime.now().strftime("%Y-%m-%d")),
    }

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "active": "dashboard",
        "stats": stats,
        "agents": agents,
        "jobs": jobs,
        "recent_runs": recent_runs,
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
            diff = datetime.now() - a.last_seen
            is_online = diff.total_seconds() < 120

        agents.append({
            **a.__dict__,
            "status": "online" if is_online else "offline",
            "last_seen_ago": time_ago(a.last_seen),
        })

    return templates.TemplateResponse("agents.html", {
        "request": request,
        "active": "agents",
        "agents": agents,
    })


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request):
    """Jobs management page."""
    storage = get_storage(request)

    jobs_raw = storage.list_jobs()
    jobs = []

    for j in jobs_raw:
        latest = storage.get_latest_run(j["name"])
        jobs.append({
            **j,
            "last_status": latest["status"] if latest else None,
        })

    return templates.TemplateResponse("jobs.html", {
        "request": request,
        "active": "jobs",
        "jobs": jobs,
    })


@router.get("/jobs/new", response_class=HTMLResponse)
async def jobs_new_page(request: Request):
    """New job form page."""
    storage = get_storage(request)
    agents = storage.list_clients()

    return templates.TemplateResponse("jobs_new.html", {
        "request": request,
        "active": "jobs",
        "agents": [{"id": a.id, "name": a.name} for a in agents],
    })


@router.post("/jobs/create")
async def jobs_create(
    request: Request,
    name: str = Form(...),
    source_path: str = Form(...),
    destination_path: str = Form(...),
    backend: str = Form("rclone"),
    client_id: str = Form(""),
    excludes: str = Form(""),
    schedule_cron: str = Form(""),
):
    """Create a new backup job."""
    storage = get_storage(request)

    job_config = {
        "source_path": source_path,
        "destination_path": destination_path,
        "backend": backend,
        "client_id": client_id if client_id else None,
        "excludes": [e.strip() for e in excludes.split(",") if e.strip()],
        "schedule_cron": schedule_cron if schedule_cron else None,
        "enabled": True,
    }

    storage.save_job(name, job_config)

    return RedirectResponse("/jobs", status_code=303)


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    """Backup history page."""
    storage = get_storage(request)

    jobs = storage.list_jobs()
    all_runs = []

    for job in jobs:
        runs = storage.get_job_runs(job["name"], limit=50)
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
                except ValueError:
                    pass

            all_runs.append({
                **run,
                "started_at_formatted": started[:19].replace("T", " ") if started else "-",
                "started_at_ago": time_ago(started),
                "duration": duration,
                "bytes_formatted": format_bytes(run.get("bytes_transferred", 0)),
            })

    # Sort by time, most recent first
    all_runs.sort(key=lambda x: x.get("started_at", ""), reverse=True)

    return templates.TemplateResponse("history.html", {
        "request": request,
        "active": "history",
        "runs": all_runs[:100],
    })


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Settings page."""
    return templates.TemplateResponse("base.html", {
        "request": request,
        "active": "settings",
    })
