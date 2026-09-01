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


def _read(path: Path) -> dict[str, object]:
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(values, dict):
        return {}
    return values


def _write(path: Path, values: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".schedule.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(values, file, indent=2)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _pause(values: dict[str, object]) -> tuple[bool, datetime | None]:
    pause = values.get("pause")
    if not isinstance(pause, dict) or not pause.get("paused"):
        return False, None
    until = pause.get("until")
    if not isinstance(until, str):
        return True, None
    try:
        return True, datetime.fromisoformat(until.replace("Z", "+00:00"))
    except ValueError:
        return True, None


def _fires(data_dir: Path) -> dict[str, object]:
    """Read the flat fire-time contract and migrate the one wrapped legacy form."""
    path = data_dir / "schedule.json"
    values = _read(path)
    legacy_fires = values.get("fires")
    if isinstance(legacy_fires, dict):
        pause = values.get("pause")
        if isinstance(pause, dict):
            _write(data_dir / "schedule-runtime.json", {"pause": pause})
        _write(path, legacy_fires)
        return legacy_fires
    return values


def schedule_pause(data_dir: Path, paused: bool, until: datetime | None) -> None:
    """Atomically persist the local scheduler pause beside its other runtime state."""
    path = data_dir / "schedule-runtime.json"
    values = _read(path)
    values["pause"] = {"paused": paused, "until": _iso(until) if paused and until else None}
    _write(path, values)


def schedule_pause_state(data_dir: Path) -> tuple[bool, datetime | None]:
    """Return the raw durable pause selection for the tray and rollback path."""
    _fires(data_dir)
    return _pause(_read(data_dir / "schedule-runtime.json"))


def scheduling_paused(data_dir: Path, now: datetime) -> bool:
    """Whether the durable local pause still covers ``now``."""
    paused, until = schedule_pause_state(data_dir)
    if not paused:
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if until is None:
        return True
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    if until > now:
        return True
    schedule_pause(data_dir, False, None)
    return False


def due_jobs(config: BackerConfig, now: datetime, data_dir: Path) -> list[str]:
    """Return due jobs and persist their fire time before a caller starts work."""
    if scheduling_paused(data_dir, now):
        return []
    schedule_path = data_dir / "schedule.json"
    fires = _fires(data_dir)
    due: list[str] = []
    for name, job in config.jobs.items():
        if not job.enabled or not job.schedule or not job.schedule.cron:
            continue
        last = (
            datetime.fromisoformat(str(fires[name]).replace("Z", "+00:00"))
            if name in fires
            else now.replace(year=now.year - 1)
        )
        if croniter(job.schedule.cron, last).get_next(datetime) <= now:
            fires[name] = _iso(now)
            due.append(name)
    if due:
        _write(schedule_path, fires)
    return due
