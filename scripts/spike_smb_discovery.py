"""Record a bounded SMB discovery or rclone transport attempt."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import click

from backer.core.mounts import SMBConnectionManager
from backer.core.smb_browse import SMBBrowser


class ArgvLeakError(RuntimeError):
    """A recoverable storage secret was placed on a command line."""


_INLINE_CREDENTIAL = re.compile(r"(?i)(?:^|[,;])(?:user(?:name)?|pass(?:word)?|domain)=[^,\s]*")
_INLINE_PASS = re.compile(r"(?i)(pass(?:word)?=)[^,:\s]*")


def sanitize(value: str | None, *secrets: str | None) -> str | None:
    """Make records safe even when a child process echoes a secret."""
    if value is None:
        return None
    clean = _INLINE_PASS.sub(r"\1[redacted]", value)
    for secret in secrets:
        if secret:
            clean = clean.replace(secret, "[redacted]")
    return clean


def assert_argv_safe(argv: list[str], password: str | None, obscured_password: str | None = None) -> None:
    """Reject plaintext or rclone-obscured passwords before spawning a child."""
    joined = " ".join(argv)
    if (
        _INLINE_CREDENTIAL.search(joined)
        or (password and password in joined)
        or (obscured_password and obscured_password in joined)
    ):
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


def _write_record(arm: str, device_label: str, dialect: str, record: dict[str, Any], *secrets: str | None) -> None:
    path = _record_path(arm, device_label, dialect)
    path.parent.mkdir(exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(sanitize(json.dumps(record, sort_keys=True), *secrets) + "\n")


def _base_record(arm: str, server: str, share: str | None, device_label: str, dialect: str) -> dict[str, Any]:
    return {
        "arm": arm,
        "platform": platform.platform(),
        "dialect": dialect,
        "device_label": device_label,
        "server": server,
        "share": sanitize(share),
        "share_count": None,
        "directory_count": None,
        "repository_size": None,
        "elapsed_ms": {},
        "error": None,
        "argv_leak": False,
    }


def _arm_a(
    record: dict[str, Any], server: str, share: str | None, username: str | None, password: str | None, depth: int
) -> None:
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
        manager = SMBConnectionManager()
        unc = f"\\\\{server}\\{share}"
        preexisting = _run(["net", "use", unc], password).returncode == 0
        started = time.perf_counter()
        record["native_session"] = manager.connect(server, share, username, password)
        record["elapsed_ms"]["native_session"] = round((time.perf_counter() - started) * 1000)
        if username and password:
            started = time.perf_counter()
            stdin_auth = _run(
                ["net", "use", unc, f"/user:{username}", "*", "/persistent:no"], password, input=f"{password}\n"
            )
            record["elapsed_ms"]["stdin_auth"] = round((time.perf_counter() - started) * 1000)
            record["stdin_auth"] = stdin_auth.returncode == 0
            if stdin_auth.returncode == 0:
                _run(["net", "use", unc, "/delete", "/y"], password)
        started = time.perf_counter()
        try:
            record["directory_count"] = len(list(Path(f"//{server}/{share}").iterdir()))
        except OSError as error:
            record["error"] = str(error)
        record["elapsed_ms"]["list_directories"] = round((time.perf_counter() - started) * 1000)
        if record["native_session"] and not preexisting:
            manager.disconnect(server, share)


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


def _create_workload(root: Path, workload_bytes: int, file_count: int) -> Path:
    source = root / "source with spaces" / "non-ascii-ä"
    source.mkdir(parents=True)
    each_file, remainder = divmod(workload_bytes, file_count)
    for index in range(file_count):
        size = each_file + (1 if index < remainder else 0)
        with (source / f"file-{index:05d}").open("wb") as handle:
            handle.write(b"x" * size)
    return source


def _timed(record: dict[str, Any], operation: str, runner: Any, argv: list[str], *args: Any, **kwargs: Any) -> Any:
    started = time.perf_counter()
    result = runner(argv, *args, **kwargs)
    record["elapsed_ms"][operation] = round((time.perf_counter() - started) * 1000)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"{operation} failed")
    return result


def run_arm_d_workload(
    record: dict[str, Any],
    server: str,
    share: str,
    username: str | None,
    password: str,
    obscured_password: str,
    runner: Any,
    workspace: Path,
    config_path: Path,
    *,
    workload_bytes: int = 5 * 1024 * 1024 * 1024,
    file_count: int = 50_000,
) -> None:
    """Exercise the real rclone provider lifecycle; callers own temporary cleanup."""
    remote = f":smb:{share}"
    environment = {
        **os.environ,
        "KOPIA_CONFIG_PATH": str(config_path),
        "KOPIA_PERSIST_CREDENTIALS_ON_CONNECT": "false",
        "RCLONE_SMB_HOST": server,
        "RCLONE_SMB_USER": username or "",
        "RCLONE_SMB_PASS": password,
    }
    create = ["kopia", "repository", "create", "rclone", f"--remote-path={remote}"]
    connect = ["kopia", "repository", "connect", "rclone", f"--remote-path={remote}"]
    _timed(record, "repository_create", runner, create, password, obscured_password, env=environment)
    _timed(record, "repository_connect", runner, connect, password, obscured_password, env=environment)
    config = config_path.read_text(encoding="utf-8", errors="replace") if config_path.exists() else ""
    if sanitize(config, password, obscured_password) != config:
        raise RuntimeError("credential persisted in repository config")
    source = _create_workload(workspace, workload_bytes, file_count)
    _timed(
        record,
        "snapshot_create",
        runner,
        ["kopia", "snapshot", "create", str(source)],
        password,
        obscured_password,
        env=environment,
    )
    listing = _timed(
        record,
        "snapshot_list",
        runner,
        ["kopia", "snapshot", "list", "--json", "--all"],
        password,
        obscured_password,
        env=environment,
    )
    snapshots = json.loads(listing.stdout or "[]")
    snapshot_id = snapshots[0].get("id") if snapshots else None
    if not snapshot_id:
        raise RuntimeError("snapshot list returned no snapshot id")
    restore = workspace / "restore"
    _timed(
        record,
        "snapshot_restore",
        runner,
        ["kopia", "snapshot", "restore", snapshot_id, str(restore)],
        password,
        obscured_password,
        env=environment,
    )
    _timed(
        record,
        "snapshot_verify",
        runner,
        ["kopia", "snapshot", "verify", "--verify-files-percent=5", snapshot_id],
        password,
        obscured_password,
        env=environment,
    )
    _timed(
        record,
        "snapshot_prune",
        runner,
        ["kopia", "snapshot", "expire", "--delete", str(source)],
        password,
        obscured_password,
        env=environment,
    )
    record["repository_size"] = config_path.stat().st_size if config_path.exists() else None


def _arm_d(record: dict[str, Any], server: str, share: str | None, username: str | None, password: str | None) -> None:
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
    with tempfile.TemporaryDirectory(prefix="backer_smb_spike_") as temporary:
        temporary_path = Path(temporary)
        run_arm_d_workload(
            record,
            server,
            share,
            username,
            password,
            obscured_password,
            _run,
            temporary_path,
            temporary_path / "repository.config",
        )


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
            _arm_a(record, server, share, username, password, depth)
        elif arm == "b":
            _arm_b(record, server, share, username, password, depth)
        else:
            _arm_d(record, server, share, username, password)
    except ArgvLeakError as error:
        record["argv_leak"] = True
        record["error"] = sanitize(str(error), password)
        _write_record(arm, device_label, dialect, record, password)
        raise click.ClickException("argv_leak") from error
    except Exception as error:
        record["error"] = sanitize(f"unreachable: {error}", password)
    _write_record(arm, device_label, dialect, record, password)


if __name__ == "__main__":
    main()
