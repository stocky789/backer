"""The serverless GUI support contract, earned by mandatory CI cells."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

# Changed only with the matching mandatory CI cells in release-validation.yml.
PROVEN_SERVERLESS_CELLS: frozenset[tuple[str, str]] = frozenset(
    (platform, kind) for platform in ("linux", "win32") for kind in ("local", "smb", "s3")
)

_JOB_TYPES = {
    "serverless-local": "local",
    "serverless-smb-linux": "smb",
    "serverless-smb-windows": "smb",
    "s3-contract": "s3",
}

_SERVERLESS_JOBS = frozenset(_JOB_TYPES)
_S3_ENV = frozenset(
    ("BACKER_TEST_S3_ENDPOINT", "BACKER_TEST_S3_BUCKET", "BACKER_TEST_S3_ACCESS_KEY", "BACKER_TEST_S3_SECRET_KEY")
)
_SMB_ENV = frozenset(
    ("BACKER_TEST_SMB_SERVER", "BACKER_TEST_SMB_SHARE", "BACKER_TEST_SMB_USERNAME", "BACKER_TEST_SMB_PASSWORD")
)


def _has_test_step(
    job: dict, command: str, *, environment: frozenset[str] = frozenset(), condition: str | None = None
) -> bool:
    return any(
        isinstance(step, dict)
        and str(step.get("run", "")).strip() == command
        and environment <= set(step.get("env", {}))
        and (condition is None or step.get("if") == condition)
        for step in job.get("steps", [])
    )


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
    # A partial release workflow has not earned any advertised local surface.
    # ponytail: all-or-nothing support; split once platforms can be released independently.
    if (
        not _SERVERLESS_JOBS <= required
        or not _SERVERLESS_JOBS <= set(jobs)
        or not _SERVERLESS_JOBS <= {result_jobs.get(result) for result in mandatory_results}
    ):
        return frozenset()

    cells: set[tuple[str, str]] = set()
    for job_name in _SERVERLESS_JOBS:
        job = jobs.get(job_name, {})
        if job_name == "s3-contract":
            if not (
                _has_test_step(
                    job, "python -m pytest -q tests/test_s3.py::test_s3_minio_end_to_end", environment=_S3_ENV
                )
                and _has_test_step(job, "python -m pytest -q tests/test_serverless_e2e.py -k s3", environment=_S3_ENV)
            ):
                return frozenset()
        elif job_name == "serverless-local":
            if not (
                _has_test_step(job, "python -m pytest -q tests/test_serverless_e2e.py -k local")
                and _has_test_step(
                    job,
                    "xvfb-run -a python -m pytest -q tests/test_gui_serverless.py",
                    condition="matrix.os == 'ubuntu-latest'",
                )
            ):
                return frozenset()
        elif job_name == "serverless-smb-linux":
            if job.get("runs-on") != "ubuntu-latest" or not _has_test_step(
                job, "sudo -E python -m pytest -q tests/test_serverless_e2e.py -k smb_linux", environment=_SMB_ENV
            ):
                return frozenset()
        elif job_name == "serverless-smb-windows" and (
            job.get("runs-on") != "windows-latest"
            or not _has_test_step(
                job, "python -m pytest -q tests/test_serverless_e2e.py -k smb_windows", environment=_SMB_ENV
            )
        ):
            return frozenset()
        matrix = job.get("strategy", {}).get("matrix", {}).get("os")
        systems = matrix if isinstance(matrix, list) else [job.get("runs-on")]
        if job_name in {"serverless-local", "s3-contract"} and {"ubuntu-latest", "windows-latest"} - set(systems):
            return frozenset()
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
