"""The serverless GUI support contract, earned by mandatory CI cells."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

# Phase 7 changes this only in the same commit that makes each matching CI cell mandatory.
PROVEN_SERVERLESS_CELLS: frozenset[tuple[str, str]] = frozenset()

_JOB_TYPES = {
    "serverless-local": "local",
    "serverless-smb-linux": "smb",
    "serverless-smb-windows": "smb",
    "s3-contract": "s3",
}


def workflow_cells(path: Path) -> frozenset[tuple[str, str]]:
    """Read mandatory serverless matrix cells from the release workflow structure."""
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    jobs = document.get("jobs", {})
    release = jobs.get("release-artifacts-ready", {})
    required = set(release.get("needs", []))
    steps = release.get("steps", [])
    script = "\n".join(str(step.get("run", "")) for step in steps if isinstance(step, dict))
    loop = re.search(r"for job in ([A-Z0-9_ ]+); do", script)
    mandatory_results = set(loop.group(1).split()) if loop else set()
    env = next((step.get("env", {}) for step in steps if isinstance(step, dict) and "env" in step), {})
    result_jobs = {
        variable: match.group(1)
        for variable, value in env.items()
        if (match := re.search(r"needs\.([\w-]+)\.result", str(value)))
    }
    cells: set[tuple[str, str]] = set()
    for result in mandatory_results:
        job_name = result_jobs.get(result)
        if job_name not in required or job_name not in _JOB_TYPES:
            continue
        job = jobs.get(job_name, {})
        if job_name == "s3-contract":
            test_steps = [
                step
                for step in job.get("steps", [])
                if isinstance(step, dict) and "pytest" in str(step.get("run"))
            ]
            if not test_steps or "BACKER_TEST_S3_BUCKET" not in test_steps[-1].get("env", {}):
                continue
        matrix = job.get("strategy", {}).get("matrix", {}).get("os")
        systems = matrix if isinstance(matrix, list) else [job.get("runs-on")]
        for system in systems:
            if system == "windows-latest":
                cells.add(("win32", _JOB_TYPES[job_name]))
            elif system == "ubuntu-latest":
                cells.add(("linux", _JOB_TYPES[job_name]))
    return frozenset(cells)


def supported_repository_types(platform: str | None = None) -> tuple[str, ...]:
    platform = platform or sys.platform
    platform = "linux" if platform.startswith("linux") else platform
    return tuple(kind for kind in ("local", "smb", "s3") if (platform, kind) in PROVEN_SERVERLESS_CELLS)
