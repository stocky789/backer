"""Local run-history storage."""

import json
import os
import tempfile
from pathlib import Path

from backer.core.job import JobRun
from backer.core.paths import get_job_subfolder


def append_run(data_dir: Path, run: JobRun) -> None:
    filename = get_job_subfolder(run.job_name)
    path = data_dir / "runs" / filename / f"{run.run_id}.json"
    _write_json(path, run.to_dict())
    latest = data_dir / "last_attempt" / f"{filename}.json"
    _write_json(latest, run.to_dict())


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(value, file)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_runs(data_dir: Path, job: str, limit: int) -> list[JobRun]:
    directory = data_dir / "runs" / get_job_subfolder(job)
    runs: list[JobRun] = []
    for path in sorted(directory.glob("*.json"), reverse=True) if directory.exists() else []:
        try:
            runs.append(JobRun.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
    return runs[:limit]
