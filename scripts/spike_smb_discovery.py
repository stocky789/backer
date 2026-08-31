"""Record a bounded SMB discovery or rclone transport attempt."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import click

from backer.core.smb_browse import SMBBrowser


class ArgvLeakError(RuntimeError):
    """A recoverable storage secret was placed on a command line."""


def assert_argv_safe(argv: list[str], password: str | None, obscured_password: str | None = None) -> None:
    """Reject plaintext or rclone-obscured passwords before spawning a child."""
    if not password:
        return
    joined = " ".join(argv)
    if password in joined or (obscured_password and obscured_password in joined):
        raise ArgvLeakError("argv_leak")


def _run(
    argv: list[str], password: str | None, obscured_password: str | None = None, **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    assert_argv_safe(argv, password, obscured_password)
    return subprocess.run(argv, capture_output=True, text=True, check=False, **kwargs)


def _obscure_password(password: str) -> str:
    result = _run(["rclone", "obscure"], password, input=password)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "rclone obscure failed")
    return result.stdout.strip()


def _record_path(arm: str, device_label: str, dialect: str) -> Path:
    safe_label = device_label.replace("/", "_").replace("\\", "_")
    safe_dialect = dialect.replace("/", "_").replace("\\", "_")
    return Path("spike-results") / f"{arm}-{safe_label}-{safe_dialect}.jsonl"


def _write_record(arm: str, device_label: str, dialect: str, record: dict[str, Any]) -> None:
    path = _record_path(arm, device_label, dialect)
    path.parent.mkdir(exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _base_record(arm: str, server: str, share: str | None, device_label: str, dialect: str) -> dict[str, Any]:
    return {
        "arm": arm,
        "platform": platform.platform(),
        "dialect": dialect,
        "device_label": device_label,
        "server": server,
        "share": re.sub(r"(pass=)[^,:/]+", r"\1[redacted]", share) if share else None,
        "share_count": None,
        "directory_count": None,
        "repository_size": None,
        "elapsed_ms": {},
        "error": None,
        "argv_leak": False,
    }


def _arm_a(record: dict[str, Any], server: str, share: str | None, password: str | None, depth: int) -> None:
    if sys.platform != "win32":
        record["error"] = "unreachable: arm A requires Windows"
        return
    started = time.perf_counter()
    result = _run(["net", "view", f"\\\\{server}"], password)
    record["elapsed_ms"]["list_shares"] = round((time.perf_counter() - started) * 1000)
    if result.returncode:
        record["error"] = result.stderr.strip() or result.stdout.strip() or "net view failed"
        return
    shares = [line.split()[0] for line in result.stdout.splitlines() if line and not line.startswith(("Share", "-"))]
    record["share_count"] = len(shares)
    if share and depth:
        started = time.perf_counter()
        try:
            record["directory_count"] = len(list(Path(f"//{server}/{share}").iterdir()))
        except OSError as error:
            record["error"] = str(error)
        record["elapsed_ms"]["list_directories"] = round((time.perf_counter() - started) * 1000)


def _arm_b(
    record: dict[str, Any],
    server: str,
    share: str | None,
    username: str | None,
    password: str | None,
    depth: int,
) -> None:
    started = time.perf_counter()
    success, result = SMBBrowser.list_shares(server, username, password)
    record["elapsed_ms"]["list_shares"] = round((time.perf_counter() - started) * 1000)
    if not success:
        record["error"] = f"unreachable: {result}"
        return
    record["share_count"] = len(result)
    if share and depth:
        started = time.perf_counter()
        success, result = SMBBrowser.list_directory(server, share, username=username, password=password)
        record["elapsed_ms"]["list_directories"] = round((time.perf_counter() - started) * 1000)
        if success:
            record["directory_count"] = len(result)
        else:
            record["error"] = f"unreachable: {result}"


def _arm_d(
    record: dict[str, Any], server: str, share: str | None, username: str | None, password: str | None
) -> None:
    if not share:
        record["error"] = "unreachable: --share is required for arm D"
        return
    # Test the supplied remote before tool availability so deliberate inline secrets fail closed.
    assert_argv_safe(["kopia", "repository", "create", "rclone", f"--remote-path={share}"], password)
    if not shutil.which("rclone"):
        record["error"] = "unreachable: rclone is not installed"
        return
    if not shutil.which("kopia"):
        record["error"] = "unreachable: kopia is not installed"
        return
    if not password:
        record["error"] = "unreachable: SPIKE_SMB_PASS is not set"
        return
    obscured_password = _obscure_password(password)
    remote = f":smb:{share}"
    argv = ["kopia", "repository", "create", "rclone", f"--remote-path={remote}"]
    started = time.perf_counter()
    result = _run(
        argv,
        password,
        obscured_password,
        env={
            **os.environ,
            "RCLONE_SMB_HOST": server,
            "RCLONE_SMB_USER": username or "",
            "RCLONE_SMB_PASS": password,
        },
    )
    record["elapsed_ms"]["repository_create"] = round((time.perf_counter() - started) * 1000)
    if result.returncode:
        record["error"] = result.stderr.strip() or result.stdout.strip() or "kopia repository create failed"


@click.command()
@click.option("--arm", type=click.Choice(["a", "b", "d"]), required=True)
@click.option("--server", required=True)
@click.option("--share")
@click.option("--device-label", default="local")
@click.option("--dialect", default="default")
@click.option("--depth", default=1, type=click.IntRange(min=0))
def main(arm: str, server: str, share: str | None, device_label: str, dialect: str, depth: int) -> None:
    """Append one bounded attempt; operational failures are recorded, not hidden."""
    password = os.environ.get("SPIKE_SMB_PASS")
    username = os.environ.get("SPIKE_SMB_USER")
    record = _base_record(arm, server, share, device_label, dialect)
    try:
        if arm == "a":
            _arm_a(record, server, share, password, depth)
        elif arm == "b":
            _arm_b(record, server, share, username, password, depth)
        else:
            _arm_d(record, server, share, username, password)
    except ArgvLeakError as error:
        record["argv_leak"] = True
        record["error"] = str(error)
        _write_record(arm, device_label, dialect, record)
        raise click.ClickException("argv_leak") from error
    except Exception as error:
        record["error"] = f"unreachable: {error}"
    _write_record(arm, device_label, dialect, record)


if __name__ == "__main__":
    main()
