"""Local run-history storage."""

import json
from pathlib import Path

from backer.core.job import JobRun
from backer.core.paths import get_job_subfolder


def append_run(data_dir: Path, run: JobRun) -> None:
    filename = get_job_subfolder(run.job_name)
    path = data_dir / "runs" / f"{filename}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(run.to_dict()) + "\n")
    latest = data_dir / "last_attempt" / f"{filename}.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    temporary = latest.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(run.to_dict()), encoding="utf-8")
        temporary.replace(latest)
    finally:
        temporary.unlink(missing_ok=True)


def read_runs(data_dir: Path, job: str, limit: int) -> list[JobRun]:
    path = data_dir / "runs" / f"{get_job_subfolder(job)}.jsonl"
    if not path.exists():
        return []
    runs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            runs.append(JobRun.from_dict(json.loads(line)))
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
    return list(reversed(runs[-limit:]))
