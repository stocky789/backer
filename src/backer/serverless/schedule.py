"""Persistent, no-daemon schedule selection."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from croniter import croniter

from backer.core.config import BackerConfig


@contextmanager
def run_lock(data_dir: Path):
    """Yield whether this process owns the one local serverless-run lock."""
    path = data_dir / "run.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    file = path.open("a+b")
    try:
        file.write(b"\0")
        file.flush()
        try:
            if sys.platform == "win32":
                import msvcrt

                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            if sys.platform == "win32":
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
    finally:
        file.close()


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
