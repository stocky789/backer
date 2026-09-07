"""Fixed-entrypoint selected-job probe for temporary privileged schedulers."""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from pathlib import Path

from backer.core import keystore
from backer.core.config import BackerConfig, ScheduleConfig
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


def wait_for_scheduled_attempt(
    previous_id: str | None, read, *, token: str | None = None, timeout: float = 65, sleep=time.sleep
) -> tuple[bool, str]:
    """Wait for the scheduled identity's next persisted attempt, never its launch acknowledgement."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        attempts = read()
        current = next(
            (
                item
                for item in attempts
                if item.get("run_id") != previous_id and (token is None or str(item.get("run_id", "")).endswith(token))
            ),
            None,
        )
        if current:
            if current.get("status") == "success":
                return True, "Scheduled run completed"
            return False, current.get("error_message") or "Scheduled run failed"
        sleep(1)
    return False, "Scheduled identity did not write an attempt record"


def prepare_scheduled_test(config: BackerConfig, job_name: str, token: str) -> tuple[Path, list[str]]:
    """Build a one-job machine-only config; task commands never receive user-controlled names."""
    job = config.jobs.get(job_name)
    if not job or not (record := config.repositories.get(job.repository)):
        raise ValueError("Selected job is not configured")
    directory = test_directory(token)
    if directory.exists():
        raise ValueError("Scheduled test context already exists")
    clone = config.model_copy(deep=True)
    clone.jobs = {job_name: job.model_copy(update={"schedule": ScheduleConfig(cron="* * * * *")})}
    clone.repositories = {job.repository: record}
    refs = []
    try:
        copied = {}
        for field in ("passphrase_ref", "storage_password_ref"):
            reference = getattr(record, field)
            if not reference:
                continue
            value = keystore.get(reference) or keystore.get(reference, machine_scope=True)
            if value is None:
                raise ValueError(f"Repository '{record.name}' secret cannot be read")
            temporary = f"backer/scheduled-test/{token}/{field}"
            keystore.put(temporary, value, machine_scope=True)
            refs.append(temporary)
            copied[field] = temporary
        clone.repositories[job.repository] = record.model_copy(update={**copied, "scope": "machine"})
        directory.mkdir(parents=True)
        clone.save(directory / "config.yaml")
        return directory, refs
    except Exception as error:
        cleanup_errors = []
        for reference in refs:
            try:
                keystore.delete(reference, machine_scope=True)
            except Exception as cleanup_error:
                cleanup_errors.append(str(cleanup_error))
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            cleanup_errors.append(str(cleanup_error))
        if cleanup_errors:
            raise RuntimeError(f"{error}; cleanup failed: {'; '.join(cleanup_errors)}") from error
        raise


def remove_scheduled_test(directory: Path, refs: list[str]) -> list[str]:
    errors = []
    for reference in refs:
        try:
            keystore.delete(reference, machine_scope=True)
        except Exception as error:
            errors.append(str(error))
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        pass
    except OSError as error:
        errors.append(str(error))
    return errors


def retry_scheduled_test_cleanup() -> list[str]:
    """Safely reap interrupted privileged test contexts before the next test starts."""
    root = get_machine_config_dir() / "scheduled-tests"
    if not root.exists():
        return []
    errors = []
    for directory in root.iterdir():
        token = directory.name
        valid_token = len(token) == 12 and all(character in "0123456789abcdef" for character in token)
        if not directory.is_dir() or not valid_token:
            continue
        try:
            config = BackerConfig.load(directory / "config.yaml")
            refs = [
                ref
                for repository in config.repositories.values()
                for ref in (repository.passphrase_ref, repository.storage_password_ref)
                if ref and ref.startswith(f"backer/scheduled-test/{token}/")
            ]
            if sys.platform == "win32":
                from backer.client.windows_service import remove_local_scheduled_test_task

                stopped, message = remove_local_scheduled_test_task(token)
            else:
                from backer.client.windows_service import remove_local_systemd_test_service

                stopped, message = remove_local_systemd_test_service(token)
            if not stopped:
                errors.append(f"{token}: {message}")
                continue
            errors.extend(f"{token}: {error}" for error in remove_scheduled_test(directory, refs))
        except Exception as error:
            errors.append(f"{token}: {error}")
    return errors


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(2)
    raise SystemExit(run(sys.argv[1]))
