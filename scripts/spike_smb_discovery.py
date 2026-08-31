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
from pathlib import PureWindowsPath
from typing import Any

import click

from backer.core.mounts import SMBConnectionManager
from backer.core.smb_browse import SMBBrowser


class ArgvLeakError(RuntimeError):
    """A recoverable storage secret was placed on a command line."""


_INLINE_CREDENTIAL = re.compile(r"(?i)(?:^|[,;])(?:user(?:name)?|pass(?:word)?|domain)=[^,\s]*")
_INLINE_PASS = re.compile(r"(?i)(pass(?:word)?=)[^,\s]*")


def sanitize(value: str | None, *secrets: str | None) -> str | None:
    """Make records safe even when a child process echoes a secret."""
    if value is None:
        return None
    clean = value
    for secret in secrets:
        if secret:
            clean = clean.replace(secret, "[redacted]")
    return _INLINE_PASS.sub(r"\1[redacted]", clean)


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
        safe_runner = lambda argv, **kwargs: _run(argv, password, **kwargs)
        record["native_session"] = bool(username and password) and manager.connect_with_stdin(
            server, share, username, password, safe_runner
        )
        record["elapsed_ms"]["native_session"] = round((time.perf_counter() - started) * 1000)
        record["stdin_auth"] = record["native_session"]
        record["elapsed_ms"]["stdin_auth"] = record["elapsed_ms"]["native_session"]
        started = time.perf_counter()
        try:
            record["directory_count"] = len(list(Path(f"//{server}/{share}").iterdir()))
        except OSError as error:
            record["error"] = str(error)
        record["elapsed_ms"]["list_directories"] = round((time.perf_counter() - started) * 1000)
        if record["native_session"] and not preexisting:
            _run(["net", "use", unc, "/delete", "/y"], password)


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


def _is_unc_path(value: str) -> bool:
    path = PureWindowsPath(value)
    return value.startswith("\\\\") and path.drive.startswith("\\\\") and "\\" in path.drive.lstrip("\\")


def _mount_source(path: Path, mount_lines: list[str] | None = None) -> str | None:
    """Return the CIFS/SMB source for the mount containing path."""
    if mount_lines is None:
        try:
            mount_lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
    candidate = str(path).replace("\\", "/").rstrip("/")
    matches: list[tuple[int, str]] = []
    for line in mount_lines:
        fields = line.split()
        if len(fields) < 3 or fields[2].casefold() not in {"cifs", "smb3"}:
            continue
        source, target = fields[0].replace("\\040", " "), fields[1].replace("\\040", " ")
        target = target.rstrip("/")
        if candidate == target or candidate.startswith(f"{target}/"):
            matches.append((len(target), source))
    return max(matches, default=(0, None))[1]


def _matches_share(source: str, server: str, share: str) -> bool:
    parts = source.replace("\\", "/").lstrip("/").split("/")
    return len(parts) >= 2 and parts[0].casefold() == server.casefold() and parts[1].casefold() == share.casefold()


def comparison_path(
    value: str,
    *,
    is_dir: Any = None,
    mount_source: Any = None,
) -> Path | None:
    """Accept only a reachable UNC path or CIFS/SMB mount."""
    path = Path(value)
    is_dir = is_dir or path.is_dir
    mount_source = mount_source or _mount_source
    if not is_dir():
        return None
    if _is_unc_path(value) or mount_source(path):
        return path
    return None


def _controlled_comparison(record: dict[str, Any], server: str, share: str) -> Path | None:
    supplied = os.environ.get("SPIKE_SMB_COMPARISON_PATH")
    if not supplied:
        return None
    comparison = comparison_path(supplied)
    if not comparison:
        record["unc_baseline"] = "unreachable: comparison path is not a UNC path or mount"
        return None
    if _is_unc_path(supplied):
        unc = PureWindowsPath(supplied)
        unc_server, unc_share = unc.drive.lstrip("\\").split("\\", 1)
        if unc_server.casefold() != server.casefold() or unc_share.casefold() != share.casefold():
            record["unc_baseline"] = "unreachable: comparison UNC is not the selected SMB device"
            return None
    elif not _matches_share(_mount_source(comparison) or "", server, share):
        record["unc_baseline"] = "unreachable: mounted comparison source is not the selected SMB device"
        return None
    return comparison


def record_unc_baseline(record: dict[str, Any], server: str, share: str, workspace: Path, _runner: Any) -> None:
    """Record the mount/UNC comparator or the reason it cannot be run safely."""
    comparison = _controlled_comparison(record, server, share)
    if not comparison:
        if record.get("unc_baseline"):
            record["failure_observations"] = {
                "rclone_startup_timeout": "unreachable: no controlled comparison endpoint",
                "connection_drop": "unreachable: no controlled comparison endpoint",
            }
            return
        record["unc_baseline"] = "unreachable: mounted/UNC comparison path is unavailable"
        record["failure_observations"] = {
            "rclone_startup_timeout": "unreachable: no controlled comparison endpoint",
            "connection_drop": "unreachable: no controlled comparison endpoint",
        }
        return
    source = workspace / "source with spaces" / "non-ascii-ä"
    baseline = comparison / "backer-spike-unc-baseline"
    _timed(
        record,
        "unc_repository_create",
        _runner,
        ["kopia", "repository", "create", "filesystem", "--path", str(baseline)],
    )
    _timed(
        record,
        "unc_repository_connect",
        _runner,
        ["kopia", "repository", "connect", "filesystem", "--path", str(baseline)],
    )
    _timed(record, "unc_snapshot_create", _runner, ["kopia", "snapshot", "create", str(source)])
    listing = _timed(record, "unc_snapshot_list", _runner, ["kopia", "snapshot", "list", "--json", "--all"])
    snapshot_id = json.loads(listing.stdout or "[]")[0]["id"]
    _timed(
        record,
        "unc_snapshot_restore",
        _runner,
        ["kopia", "snapshot", "restore", snapshot_id, str(workspace / "unc-restore")],
    )
    _timed(
        record, "unc_snapshot_verify", _runner, ["kopia", "snapshot", "verify", "--verify-files-percent=5", snapshot_id]
    )
    _timed(record, "unc_snapshot_prune", _runner, ["kopia", "snapshot", "expire", "--delete", str(source)])
    record["unc_ratio"] = record["elapsed_ms"]["snapshot_create"] / max(record["elapsed_ms"]["unc_snapshot_create"], 1)
    record["unc_within_1_25x"] = record["unc_ratio"] <= 1.25
    record["unc_baseline"] = "completed"
    record["failure_observations"] = {
        "rclone_startup_timeout": "unreachable: no controlled failure endpoint",
        "connection_drop": "unreachable: no controlled failure endpoint",
    }


def _drop_command() -> list[str] | None:
    encoded = os.environ.get("SPIKE_SMB_DROP_COMMAND_JSON")
    if not encoded:
        return None
    command = json.loads(encoded)
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise RuntimeError("SPIKE_SMB_DROP_COMMAND_JSON must be a JSON argv array")
    return command


def _failure_evidence(result: subprocess.CompletedProcess[str], *secrets: str) -> dict[str, Any]:
    return {
        "returncode": result.returncode,
        "stdout": sanitize(result.stdout, *secrets),
        "stderr": sanitize(result.stderr, *secrets),
    }


def record_failure_observations(
    record: dict[str, Any], remote: str, password: str, obscured_password: str, runner: Any, environment: dict[str, str]
) -> None:
    """Force the two unattended failure modes only on an explicitly controlled endpoint."""
    drop_command = _drop_command()
    if not drop_command:
        record["failure_observations"] = {
            "rclone_startup_timeout": "unreachable: no controlled failure endpoint",
            "connection_drop": "unreachable: no controlled failure endpoint",
        }
        return

    observations: dict[str, Any] = {"rclone_startup_timeout": {"attempted": True}}
    started = time.perf_counter()
    timeout_result = runner(
        ["kopia", "repository", "connect", "rclone", f"--remote-path={remote}", "--rclone-startup-timeout=1ms"],
        password,
        obscured_password,
        env=environment,
    )
    observations["rclone_startup_timeout"].update(
        elapsed_ms=round((time.perf_counter() - started) * 1000),
        observed=timeout_result.returncode != 0,
        evidence=_failure_evidence(timeout_result, password, obscured_password),
    )
    if timeout_result.returncode == 0:
        observations["rclone_startup_timeout"]["error"] = "controlled timeout was not observed"

    assert_argv_safe(drop_command, password, obscured_password)
    control_result = runner(drop_command, password, obscured_password, env=environment)
    if control_result.returncode:
        raise RuntimeError(control_result.stderr.strip() or control_result.stdout.strip() or "drop control failed")
    started = time.perf_counter()
    drop_result = runner(["kopia", "snapshot", "list", "--json", "--all"], password, obscured_password, env=environment)
    observations["connection_drop"] = {
        "attempted": True,
        "control": "completed",
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "observed": drop_result.returncode != 0,
        "evidence": _failure_evidence(drop_result, password, obscured_password),
    }
    record["failure_observations"] = observations
    if not all(observation["observed"] for observation in observations.values()):
        raise RuntimeError("controlled SMB failure was not observed")


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
    record_unc_baseline(
        record,
        server,
        share,
        workspace,
        lambda argv: runner(argv, password, obscured_password, env=environment),
    )
    if record.get("unc_baseline") == "completed":
        record_failure_observations(record, remote, password, obscured_password, runner, environment)


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
    record["_secrets"] = [password, obscured_password]
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
        _write_record(arm, device_label, dialect, record, *record.pop("_secrets", [password]))
        raise click.ClickException("argv_leak") from error
    except Exception as error:
        record["error"] = sanitize(f"unreachable: {error}", password)
    _write_record(arm, device_label, dialect, record, *record.pop("_secrets", [password]))


if __name__ == "__main__":
    main()
