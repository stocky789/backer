"""Fixed-entrypoint selected-job probe for temporary privileged schedulers."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from backer.core.config import BackerConfig
from backer.core.paths import get_machine_config_dir
from backer.serverless.runs import run_due_jobs

_TOKEN = re.compile(r"[0-9a-f]{12}\Z")


def test_directory(token: str) -> Path:
    if not _TOKEN.fullmatch(token):
        raise ValueError("Invalid scheduled test token")
    return get_machine_config_dir() / "scheduled-tests" / token


def run(token: str) -> int:
    directory = test_directory(token)
    config_path = directory / "config.yaml"
    if not config_path.is_file():
        raise FileNotFoundError("Scheduled test config is unavailable")
    os.environ["BACKER_CONFIG_DIR"] = str(directory)
    os.environ["BACKER_DATA_DIR"] = str(directory / "data")
    os.environ["BACKER_RUN_AS_SYSTEM"] = "1"
    os.environ["BACKER_ATTEMPT_TOKEN"] = token
    reports = run_due_jobs(BackerConfig.load(config_path), run_as_system=True)
    return 0 if reports and all(report.get("success") for report in reports) else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(2)
    raise SystemExit(run(sys.argv[1]))
