"""Persistent, no-daemon schedule selection."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from croniter import croniter

from backer.core.config import BackerConfig


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _read(path: Path) -> dict[str, str]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".schedule.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(values, file, indent=2)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def due_jobs(config: BackerConfig, now: datetime, data_dir: Path) -> list[str]:
    """Return due jobs and persist their fire time before a caller starts work."""
    schedule_path = data_dir / "schedule.json"
    fires = _read(schedule_path)
    due: list[str] = []
    for name, job in config.jobs.items():
        if not job.enabled or not job.schedule or not job.schedule.cron:
            continue
        last = (
            datetime.fromisoformat(fires[name].replace("Z", "+00:00"))
            if name in fires
            else now.replace(year=now.year - 1)
        )
        if croniter(job.schedule.cron, last).get_next(datetime) <= now:
            fires[name] = _iso(now)
            due.append(name)
    if due:
        _write(schedule_path, fires)
    return due
