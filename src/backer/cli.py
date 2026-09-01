"""Backer CLI - unified backup management."""

import json
import os
import shlex
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from secrets import choice
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

import click
from rich.console import Console
from rich.table import Table

from backer import __version__
from backer.backends.base import BackupDestination, BackupSource

console = Console()


def _interactive() -> bool:
    """Prompts are allowed only when both ends are terminals."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _missing_flags(command: str, flags: list[str]) -> None:
    """Use one predictable non-interactive refusal for incomplete commands."""
    if not flags:
        return
    rendered = " ".join(flags)
    raise click.UsageError(f"Missing {rendered}. Re-run: backer {command} {rendered}")


def _eff_wordlist_path() -> Path:
    """Find the bundled EFF list in a wheel or PyInstaller extraction directory."""
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        candidate = Path(frozen) / "backer" / "assets" / "eff_large_wordlist.txt"
        if candidate.is_file():
            return candidate
    return Path(str(files("backer").joinpath("assets/eff_large_wordlist.txt")))


def _generated_passphrase() -> str:
    words = tuple(
        line.split("\t", 1)[1]
        for line in _eff_wordlist_path().read_text(encoding="utf-8").splitlines()
        if "\t" in line
    )
    return "-".join(choice(words) for _ in range(6))


def _write_recovery_export(path: Path, name: str, repository_id: str | None, hint: str, passphrase: str) -> None:
    """Atomically save the one recovery record before removing local access."""
    payload = json.dumps(
        {
            "repository_name": name,
            "repository_id": repository_id,
            "location_hint": hint,
            "exported_at": datetime.now(UTC).isoformat(),
            "passphrase": passphrase,
        },
        sort_keys=True,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _load_recovery_passphrase(path: Path) -> str:
    """Read a recovery record without accepting malformed secret-bearing JSON."""
    value = path.read_text(encoding="utf-8").rstrip("\r\n")
    if value.startswith("{"):
        try:
            record = json.loads(value)
        except json.JSONDecodeError as error:
            raise click.UsageError("Recovery file is not valid JSON") from error
        if not isinstance(record, dict) or set(record) != {
            "repository_name", "repository_id", "location_hint", "exported_at", "passphrase"
        } or not isinstance(record["passphrase"], str):
            raise click.UsageError("Recovery file has an invalid format")
        value = record["passphrase"]
    if not value:
        raise click.UsageError("Repository passphrase is required")
    return value


def _redact_error(error: BaseException, *secrets: str | None) -> str:
    text = str(error)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _local_job_call(call: Callable[[], Any]) -> Any:
    """Keep every local job mode on the same Ctrl-C exit contract."""
    try:
        return call()
    except KeyboardInterrupt as error:
        raise click.exceptions.Exit(130) from error


def _restore_include_path(value: str | None) -> str | None:
    """Accept only a relative snapshot subtree; it must never escape the snapshot."""
    if value is None:
        return None
    candidate = Path(value)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts) or candidate.drive:
        raise click.UsageError("--include must be a relative path inside the snapshot")
    return value


def _snapshot_restore_test_files(
    backend, destination: BackupDestination, snapshot_id: str, count: int = 12
) -> list[Path]:
    """Choose the smallest regular files from the immutable snapshot tree, not the live source."""
    pending = [""]
    files: list[tuple[int, Path]] = []
    while pending:
        parent = pending.pop()
        for entry in backend.get_snapshot_files(destination, snapshot_id, path=parent):
            name = entry.get("name", "")
            relative = Path(parent, name)
            if not name or relative.is_absolute() or ".." in relative.parts:
                raise click.ClickException("Restore test found an unsafe path in the snapshot")
            if entry.get("type") == "dir":
                pending.append(relative.as_posix())
            elif entry.get("type") == "file":
                files.append((int(entry.get("size", 0)), relative))
    return [path for _, path in sorted(files, key=lambda item: (item[0], item[1].as_posix()))[:count]]


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run_restore_test(backend, destination: BackupDestination, snapshot: dict[str, Any]) -> tuple[int, int]:
    """Restore only a small snapshot sample, compare still-present source files, then clean up."""
    snapshot_id = snapshot.get("full_id") or snapshot.get("id")
    if not snapshot_id:
        raise click.ClickException("Restore test cannot identify the newest immutable snapshot")
    source = Path(snapshot["paths"][0])
    if not source.is_dir():
        raise click.ClickException("Restore test needs the original source folder on this computer")
    files = _snapshot_restore_test_files(backend, destination, snapshot_id)
    if not files:
        raise click.ClickException("Restore test found no regular source files to compare")
    compared = 0
    skipped = 0
    with TemporaryDirectory(prefix="backer-restore-test-") as temporary:
        target = Path(temporary)
        for relative in files:
            original = source / relative
            if not original.is_file():
                skipped += 1
                continue
            result = backend.restore(
                destination,
                target,
                snapshot=snapshot_id,
                include_path=relative.as_posix(),
            )
            if not result.success:
                raise click.ClickException(result.output or "; ".join(result.errors) or "Restore test failed")
            restored = target / relative
            if not restored.is_file() or _sha256(restored) != _sha256(original):
                raise click.ClickException(f"Restore test did not match {relative.as_posix()}")
            compared += 1
    if not compared:
        raise click.ClickException("Restore test could not compare a source file that still exists")
    return compared, skipped


def _select_restore_snapshot(
    rows: list[dict[str, Any]], snapshot: str | None, before: str | None, latest: bool, computer: str | None
) -> tuple[str, dict[str, Any]]:
    """Select only a listed immutable snapshot; never infer one from a basename."""
    if sum(bool(value) for value in (snapshot, before, latest)) != 1:
        raise click.UsageError("Choose exactly one of --snapshot, --before, or --latest")
    candidates = [item for item in rows if not computer or item.get("hostname") == computer]
    if before:
        candidates = [item for item in candidates if (item.get("timestamp") or "") <= before]
    if not candidates:
        raise click.ClickException("No matching immutable snapshot was found")
    selected = snapshot or max(candidates, key=lambda item: item.get("timestamp") or "")["full_id"]
    selected_row = next((item for item in candidates if selected in (item.get("id"), item.get("full_id"))), None)
    if selected_row is None:
        raise click.ClickException("The requested snapshot was not found")
    return selected, selected_row


def _restore_execute(
    backend, source_path: str, destination: Path, selected: str, *, include: str | None,
    original_source_path: str | None, progress: bool | None, passphrase: str
):
    """The one local restore execution path; callers only supply already-safe inputs."""
    try:
        with _local_progress(progress) as render:
            result = backend.restore(
                source=BackupDestination(source_path), destination=destination, snapshot=selected, include_path=include,
                original_source_path=original_source_path, progress_callback=render,
            )
    except KeyboardInterrupt as error:
        raise click.exceptions.Exit(130) from error
    if not result.success:
        raise click.ClickException(_redact_error(RuntimeError("; ".join(result.errors)), passphrase))
    return result


def _stale_cutoff(cron: str, now) -> object:
    """Return the age boundary after two complete schedule intervals."""
    from croniter import croniter

    iterator = croniter(cron, now)
    previous = iterator.get_prev(type(now))
    prior = iterator.get_prev(type(now))
    following = croniter(cron, previous).get_next(type(now))
    return now - ((following - previous) + (previous - prior))


def _is_server_data_dir(path: Path) -> bool:
    """Keep a colocated server database when uninstalling the agent."""
    return (path / "backer.db").exists()


@click.group()
@click.version_option(version=__version__)
@click.option("--config", "-c", type=click.Path(path_type=Path), help="Config file path")
@click.pass_context
def main(ctx: click.Context, config: Path | None) -> None:
    """Backer - Unified backup orchestration.

    Back up and restore with Kopia.

    Run 'backer setup' to download and install Kopia.
    """
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


# ============ Setup and tool management ============


@main.command()
@click.option("--force", "-f", is_flag=True, help="Force reinstall even if already installed")
@click.option("--quiet", "-q", is_flag=True, help="Suppress output (for scripted installs)")
def setup(force: bool, quiet: bool) -> None:
    """Download and install Kopia.

    This automatically downloads Kopia so you don't
    need to install them manually.
    """
    from backer.tools.manager import get_tool_manager

    manager = get_tool_manager()

    if not quiet:
        console.print("[bold]Backer Setup[/bold]")
        console.print(f"Tools directory: {manager.tools_dir}\n")

    tools = ["kopia"]
    failures = []

    for tool in tools:
        existing = manager.get_tool_path(tool)

        if existing and not force:
            if not quiet:
                version = manager.get_version(tool)
                console.print(f"[green]✓[/green] {tool} already installed: {version}")
                console.print(f"  Path: {existing}")
        else:
            if not quiet:
                console.print(f"[yellow]→[/yellow] Downloading {tool}...")
            try:
                progress_cb = None if quiet else lambda msg: console.print(f"  {msg}")
                path = manager.download(tool, progress_callback=progress_cb)
                if not quiet:
                    version = manager.get_version(tool)
                    console.print(f"[green]✓[/green] {tool} installed: {version}")
                    console.print(f"  Path: {path}")
            except Exception as e:
                failures.append(tool)
                if not quiet:
                    console.print(f"[red]✗[/red] Failed to install {tool}: {e}")

    if failures:
        raise click.ClickException(f"Failed to install: {', '.join(failures)}")

    if not quiet:
        console.print("\n[bold]Setup complete[/bold]")
        console.print("Run 'backer tools' to see Kopia status.")


@dataclass(frozen=True)
class InitStep:
    key: str
    flag: str
    prompt: str
    validator: Callable[[Any], bool]
    applicable: Callable[[dict[str, Any]], bool]
    required: bool | str
    serializer: Callable[[Any], list[str]]
    wizard_group: int | None = None
    render_applicable: Callable[[dict[str, Any]], bool] | None = None


def _always(_: dict[str, Any]) -> bool:
    return True


def _is_type(*types: str) -> Callable[[dict[str, Any]], bool]:
    return lambda values: values["repository_type"] in types


def _not_interactive(_: dict[str, Any]) -> bool:
    return not _interactive()


def _valid_schedule(value: Any) -> bool:
    from croniter import croniter

    return isinstance(value, str) and bool(value) and croniter.is_valid(value)


def _value(flag: str) -> Callable[[Any], list[str]]:
    return lambda value: [flag, str(value)] if value is not None and value != "" else []


def _switch(flag: str) -> Callable[[Any], list[str]]:
    return lambda value: [flag] if value else []


def _repeated(flag: str) -> Callable[[Any], list[str]]:
    return lambda value: [part for item in value for part in (flag, str(item))]


def _never(_: Any) -> list[str]:
    return []


# Shared with the later rich wizard: validation and rendering use this one flag source.
INIT_STEPS = (
    InitStep(
        "repository_type",
        "--type",
        "Where backups are stored",
        lambda value: value in {"local", "smb", "s3"},
        _always,
        True,
        _value("--type"),
        1,
    ),
    InitStep("path", "--path", "Repository path", bool, _is_type("local", "smb"), True, _value("--path"), 2),
    InitStep("host", "--host", "SMB host", bool, _is_type("smb"), True, _value("--host"), 2),
    InitStep("share", "--share", "SMB share", bool, _is_type("smb"), True, _value("--share"), 2),
    InitStep("username", "--username", "File-server user", bool, _is_type("smb"), True, _value("--username"), 2),
    InitStep(
        "password_stdin",
        "--password-stdin",
        "File-server sign-in",
        bool,
        _is_type("smb"),
        "smb-password",
        _switch("--password-stdin"),
        2,
    ),
    InitStep(
        "password_file",
        "--password-file",
        "File-server sign-in",
        lambda value: value is not None,
        _is_type("smb"),
        "smb-password",
        _value("--password-file"),
        2,
    ),
    InitStep(
        "password_env", "$BACKER_SMB_PASSWORD", "File-server sign-in", bool, _is_type("smb"), "smb-password", _never, 2
    ),
    InitStep("bucket", "--bucket", "S3 bucket", bool, _is_type("s3"), True, _value("--bucket"), 2),
    InitStep(
        "prefix", "--prefix", "S3 prefix", lambda value: value is not None, _is_type("s3"), False, _value("--prefix"), 2
    ),
    InitStep("endpoint", "--endpoint", "S3 endpoint", bool, _is_type("s3"), True, _value("--endpoint"), 2),
    InitStep("region", "--region", "S3 region", bool, _is_type("s3"), True, _value("--region"), 2),
    InitStep(
        "access_key_id", "--access-key-id", "S3 access key", bool, _is_type("s3"), True, _value("--access-key-id"), 2
    ),
    InitStep(
        "secret_key_stdin",
        "--secret-key-stdin",
        "S3 secret key",
        bool,
        _is_type("s3"),
        "s3-secret",
        _switch("--secret-key-stdin"),
        2,
    ),
    InitStep(
        "secret_key_file",
        "--secret-key-file",
        "S3 secret key",
        lambda value: value is not None,
        _is_type("s3"),
        "s3-secret",
        _value("--secret-key-file"),
        2,
    ),
    InitStep("secret_key_env", "$BACKER_S3_SECRET_KEY", "S3 secret key", bool, _is_type("s3"), "s3-secret", _never, 2),
    InitStep(
        "passphrase_stdin",
        "--passphrase-stdin",
        "Repository passphrase",
        bool,
        _not_interactive,
        "passphrase",
        _switch("--passphrase-stdin"),
        3,
    ),
    InitStep(
        "passphrase_file",
        "--passphrase-file",
        "Repository passphrase",
        lambda value: value is not None,
        _not_interactive,
        "passphrase",
        _value("--passphrase-file"),
        3,
        _always,
    ),
    InitStep(
        "generate_passphrase",
        "--generate-passphrase",
        "Generate passphrase",
        bool,
        _not_interactive,
        "passphrase",
        _switch("--generate-passphrase"),
        3,
    ),
    InitStep(
        "passphrase_out",
        "--passphrase-out",
        "Passphrase export",
        lambda value: value is not None,
        _always,
        False,
        _value("--passphrase-out"),
        3,
    ),
    InitStep(
        "print_passphrase",
        "--print-passphrase",
        "Print passphrase",
        bool,
        _always,
        False,
        _switch("--print-passphrase"),
        3,
    ),
    InitStep(
        "update_password",
        "--update-password",
        "Update storage password",
        bool,
        _always,
        False,
        _switch("--update-password"),
    ),
    InitStep(
        "update_passphrase",
        "--update-passphrase",
        "Update repository passphrase",
        bool,
        _always,
        False,
        _switch("--update-passphrase"),
    ),
    InitStep("source", "--source", "Folder to back up", bool, _always, True, _repeated("--source"), 4),
    InitStep(
        "exclude",
        "--exclude",
        "Exclude pattern",
        lambda value: value is not None,
        _always,
        False,
        _repeated("--exclude"),
        4,
    ),
    InitStep("schedule", "--schedule", "Schedule", _valid_schedule, _always, "schedule", _value("--schedule"), 5),
    InitStep("no_schedule", "--no-schedule", "No schedule", bool, _always, "schedule", _switch("--no-schedule"), 5),
    InitStep(
        "keep_last",
        "--keep-last",
        "Keep latest",
        lambda value: value is not None,
        _always,
        False,
        _value("--keep-last"),
    ),
    InitStep(
        "keep_daily",
        "--keep-daily",
        "Keep daily",
        lambda value: value is not None,
        _always,
        False,
        _value("--keep-daily"),
    ),
    InitStep(
        "keep_weekly",
        "--keep-weekly",
        "Keep weekly",
        lambda value: value is not None,
        _always,
        False,
        _value("--keep-weekly"),
    ),
    InitStep(
        "keep_monthly",
        "--keep-monthly",
        "Keep monthly",
        lambda value: value is not None,
        _always,
        False,
        _value("--keep-monthly"),
    ),
    InitStep(
        "keep_yearly",
        "--keep-yearly",
        "Keep yearly",
        lambda value: value is not None,
        _always,
        False,
        _value("--keep-yearly"),
    ),
    InitStep("repo_name", "--repo-name", "Repository name", bool, _always, True, _value("--repo-name"), 5),
    InitStep("job_name", "--job-name", "Job name", bool, _always, True, _value("--job-name"), 5),
    InitStep("install", "--install", "Install schedule", bool, _always, False, _switch("--install")),
)


def _init_missing(values: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    groups: dict[str, list[InitStep]] = {}
    for step in INIT_STEPS:
        if not step.applicable(values) or not step.required:
            continue
        if step.required is True:
            if not step.validator(values[step.key]):
                missing.append(step.flag)
        else:
            groups.setdefault(step.required, []).append(step)
    for steps in groups.values():
        if not any(step.validator(values[step.key]) for step in steps):
            missing.append(" or ".join(step.flag for step in steps if step.flag.startswith("--")))
    return missing


def _render_init_command(values: dict[str, Any]) -> str:
    rendered = dict(values)
    if rendered.get("_generated_passphrase") and rendered.get("passphrase_out"):
        rendered.update(generate_passphrase=False, passphrase_file=rendered["passphrase_out"], passphrase_out=None)
    arguments = [
        part
        for step in INIT_STEPS
        if (step.render_applicable or step.applicable)(rendered)
        for part in step.serializer(rendered[step.key])
    ]
    return "backer init " + " ".join(shlex.quote(argument) for argument in arguments)


@main.command("init")
@click.option("--type", "repository_type", type=click.Choice(["local", "smb", "s3"]))
@click.option("--path")
@click.option("--host")
@click.option("--share")
@click.option("--username")
@click.option("--password-stdin", is_flag=True)
@click.option("--password-file", type=click.Path(exists=True, path_type=Path))
@click.option("--bucket")
@click.option("--prefix")
@click.option("--endpoint")
@click.option("--region")
@click.option("--access-key-id")
@click.option("--secret-key-stdin", is_flag=True)
@click.option("--secret-key-file", type=click.Path(exists=True, path_type=Path))
@click.option("--source", multiple=True)
@click.option("--exclude", multiple=True)
@click.option("--schedule")
@click.option("--no-schedule", is_flag=True)
@click.option("--keep-last", type=int)
@click.option("--keep-daily", type=int)
@click.option("--keep-weekly", type=int)
@click.option("--keep-monthly", type=int)
@click.option("--keep-yearly", type=int)
@click.option("--repo-name", default="repository")
@click.option("--job-name", default="backup")
@click.option("--passphrase-stdin", is_flag=True)
@click.option("--passphrase-file", type=click.Path(exists=True, path_type=Path))
@click.option("--generate-passphrase", is_flag=True)
@click.option("--passphrase-out", type=click.Path(path_type=Path))
@click.option("--print-passphrase", is_flag=True)
@click.option("--update-password", is_flag=True)
@click.option("--update-passphrase", is_flag=True)
@click.option("--install", is_flag=True)
@click.pass_context
def init(
    ctx: click.Context,
    repository_type: str | None,
    path: str | None,
    host: str | None,
    share: str | None,
    username: str | None,
    password_stdin: bool,
    password_file: Path | None,
    bucket: str | None,
    prefix: str | None,
    endpoint: str | None,
    region: str | None,
    access_key_id: str | None,
    secret_key_stdin: bool,
    secret_key_file: Path | None,
    source: tuple[str, ...],
    exclude: tuple[str, ...],
    schedule: str | None,
    no_schedule: bool,
    keep_last: int | None,
    keep_daily: int | None,
    keep_weekly: int | None,
    keep_monthly: int | None,
    keep_yearly: int | None,
    repo_name: str,
    job_name: str,
    passphrase_stdin: bool,
    passphrase_file: Path | None,
    generate_passphrase: bool,
    passphrase_out: Path | None,
    print_passphrase: bool,
    update_password: bool,
    update_passphrase: bool,
    install: bool,
) -> None:
    """Start serverless setup; `setup` downloads Kopia and `agent setup` enrolls a server agent."""
    values = {
        "repository_type": repository_type,
        "path": path,
        "host": host,
        "share": share,
        "username": username,
        "password_stdin": password_stdin,
        "password_file": password_file,
        "password_env": bool(os.environ.get("BACKER_SMB_PASSWORD")),
        "bucket": bucket,
        "prefix": prefix,
        "endpoint": endpoint,
        "region": region,
        "access_key_id": access_key_id,
        "secret_key_stdin": secret_key_stdin,
        "secret_key_file": secret_key_file,
        "secret_key_env": bool(os.environ.get("BACKER_S3_SECRET_KEY")),
        "source": source,
        "exclude": exclude,
        "schedule": schedule,
        "no_schedule": no_schedule,
        "keep_last": keep_last,
        "keep_daily": keep_daily,
        "keep_weekly": keep_weekly,
        "keep_monthly": keep_monthly,
        "keep_yearly": keep_yearly,
        "repo_name": repo_name,
        "job_name": job_name,
        "passphrase_stdin": passphrase_stdin,
        "passphrase_file": passphrase_file,
        "generate_passphrase": generate_passphrase,
        "passphrase_out": passphrase_out,
        "print_passphrase": print_passphrase,
        "update_password": update_password,
        "update_passphrase": update_passphrase,
        "install": install,
    }
    if _interactive():
        from backer.client.serverless_wizard import WizardAbortError, run_wizard

        try:
            values = run_wizard(values)
        except WizardAbortError:
            ctx.exit(130)
    missing = _init_missing(values)
    if missing:
        _missing_flags("init", missing)
    repository_type = values["repository_type"]
    path = values["path"]
    host = values["host"]
    share = values["share"]
    username = values["username"]
    password_stdin = values["password_stdin"]
    password_file = values["password_file"]
    bucket = values["bucket"]
    prefix = values["prefix"]
    endpoint = values["endpoint"]
    region = values["region"]
    access_key_id = values["access_key_id"]
    secret_key_stdin = values["secret_key_stdin"]
    secret_key_file = values["secret_key_file"]
    source = values["source"]
    exclude = values["exclude"]
    schedule = values["schedule"]
    no_schedule = values["no_schedule"]
    keep_last = values["keep_last"]
    keep_daily = values["keep_daily"]
    keep_weekly = values["keep_weekly"]
    keep_monthly = values["keep_monthly"]
    keep_yearly = values["keep_yearly"]
    repo_name = values["repo_name"]
    job_name = values["job_name"]
    passphrase_stdin = values["passphrase_stdin"]
    passphrase_file = values["passphrase_file"]
    generate_passphrase = values["generate_passphrase"]
    passphrase_out = values["passphrase_out"]
    print_passphrase = values["print_passphrase"]
    update_password = values["update_password"]
    update_passphrase = values["update_passphrase"]
    install = values["install"]
    ctx.invoke(
        repo_add,
        name=repo_name,
        attach=False,
        initialize=True,
        repository_type=repository_type,
        path=path,
        server=host,
        share=share,
        username=username,
        domain=None,
        bucket=bucket,
        prefix=prefix,
        endpoint=endpoint,
        region=region,
        path_style=False,
        storage_stdin=password_stdin,
        storage_file=password_file,
        access_key_id=access_key_id,
        secret_key_stdin=secret_key_stdin,
        secret_key_file=secret_key_file,
        adopt=False,
        passphrase_stdin=passphrase_stdin,
        passphrase_file=passphrase_file,
        generate_passphrase=generate_passphrase,
        passphrase_out=passphrase_out,
        print_passphrase=print_passphrase,
        update_password=update_password,
        update_passphrase=update_passphrase,
        headless=True,
        yes=True,
        storage_password=values.get("_storage_password"),
        generated_passphrase=values.get("_generated_passphrase"),
    )
    ctx.invoke(
        job_create,
        name=job_name,
        legacy_name=None,
        source=source,
        exclude=exclude,
        dest=None,
        schedule=schedule,
        daily=None,
        no_schedule=no_schedule,
        repository_id=repo_name,
        keep_last=keep_last,
        keep_daily=keep_daily,
        keep_weekly=keep_weekly,
        keep_monthly=keep_monthly,
        keep_yearly=keep_yearly,
        server=None,
        username=None,
        password=None,
    )
    if install:
        from backer.client.windows_service import is_windows

        ctx.invoke(agent_install, mode="local", headless=False, method="service" if is_windows() else "systemd")
    click.echo(f"Run again: {_render_init_command(values)}")


@main.command()
def tools() -> None:
    """Show Kopia status."""
    from backer.tools.manager import get_tool_manager

    manager = get_tool_manager()
    tools_info = {"kopia": manager.list_tools()["kopia"]}

    table = Table(title="Kopia")
    table.add_column("Tool", style="cyan")
    table.add_column("Status")
    table.add_column("Version")
    table.add_column("Location")

    for name, info in tools_info.items():
        if info["installed"]:
            status = "[green]✓ Installed[/green]"
            if info["managed"]:
                status += " (managed)"
            else:
                status += " (system)"
        else:
            status = "[red]✗ Not installed[/red]"

        table.add_row(
            name,
            status,
            info["version"] or "-",
            info["path"] or f"(available: v{info['available_version']})",
        )

    console.print(table)
    console.print(f"\nManaged tools directory: {manager.tools_dir}")
    console.print("Run 'backer setup' to install Kopia.")


# ============ Standalone backup commands ============


@main.command()
@click.argument("source", type=click.Path(exists=True, path_type=Path))
@click.argument("destination")
@click.option("--exclude", "-e", multiple=True, help="Exclude patterns")
@click.option("--dry-run", "-n", is_flag=True, help="Simulate without making changes")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
def backup(
    source: Path,
    destination: str,
    exclude: tuple[str, ...],
    dry_run: bool,
    verbose: bool,
) -> None:
    """Run a one-off backup.

    SOURCE is the local path to back up.
    DESTINATION is where to store the backup (local path or remote URI).

    Examples:

        backer backup /home/user/docs /mnt/backup/docs

    """
    repository_password = os.environ.get("BACKER_REPOSITORY_PASSWORD")
    if not repository_password:
        raise click.UsageError("BACKER_REPOSITORY_PASSWORD is required")
    from backer.backends.kopia import KopiaBackend

    console.print(f"[bold]Backing up[/bold] {source} → {destination}")
    console.print("Kopia" + (" (dry run)" if dry_run else ""))

    try:
        be = KopiaBackend({"repository_password": repository_password})

        with console.status("Checking Kopia availability..."):
            available, msg = be.check_available()

        if not available:
            console.print(f"[red]Error:[/red] {msg}")
            raise SystemExit(1)

        console.print(f"[dim]{msg}[/dim]")

        src = BackupSource(path=source, excludes=list(exclude))
        dest = BackupDestination(path=destination)

        with console.status("Running backup..."):
            result = be.backup(source=src, destination=dest, dry_run=dry_run)

        if result.success:
            console.print("[green]✓ Backup completed successfully[/green]")
            console.print(f"  Files: {result.files_transferred}")
            console.print(f"  Bytes: {result.bytes_transferred:,}")
            console.print(f"  Duration: {result.duration_seconds:.1f}s")
        else:
            console.print("[red]✗ Backup failed[/red]")
            for error in result.errors:
                console.print(f"  [red]{error}[/red]")
            raise SystemExit(1)

        if verbose and result.output:
            console.print("\n[dim]--- Output ---[/dim]")
            console.print(result.output[:2000])

    except Exception as e:
        console.print(f"[red]Error:[/red] {_redact_error(e, repository_password)}")
        raise SystemExit(1)


@main.command()
@click.argument("source", required=False)
@click.argument("legacy_destination", required=False, type=click.Path(path_type=Path))
@click.option("--job")
@click.option("--from", "from_path")
@click.option("--passphrase-stdin", is_flag=True, help="Read a recovery repository passphrase from stdin")
@click.option("--passphrase-file", type=click.Path(path_type=Path))
@click.option("--username")
@click.option("--domain")
@click.option("--password-stdin", is_flag=True)
@click.option("--password-file", type=click.Path(path_type=Path))
@click.option("--bucket")
@click.option("--prefix")
@click.option("--endpoint")
@click.option("--region")
@click.option("--access-key-id")
@click.option("--secret-key-stdin", is_flag=True)
@click.option("--secret-key-file", type=click.Path(path_type=Path))
@click.option("--into", type=click.Choice(["NEW", "MERGE", "REPLACE"]), default=None)
@click.option("--destination", type=click.Path(path_type=Path))
@click.option("--snapshot", "-s", help="Snapshot ID to restore")
@click.option("--before")
@click.option("--latest", is_flag=True)
@click.option("--include")
@click.option("--computer")
@click.option("--yes-replace", is_flag=True)
@click.option("--clean-up-replaced", is_flag=True)
@click.option("--dry-run", "-n", is_flag=True, help="Simulate without making changes")
@click.option("--progress/--no-progress", default=None)
def restore(
    source: str | None,
    legacy_destination: Path | None,
    snapshot: str | None,
    job: str | None,
    from_path: str | None,
    passphrase_stdin: bool,
    passphrase_file: Path | None,
    username: str | None,
    domain: str | None,
    password_stdin: bool,
    password_file: Path | None,
    bucket: str | None,
    prefix: str | None,
    endpoint: str | None,
    region: str | None,
    access_key_id: str | None,
    secret_key_stdin: bool,
    secret_key_file: Path | None,
    into: str | None,
    destination: Path | None,
    before: str | None,
    latest: bool,
    include: str | None,
    computer: str | None,
    yes_replace: bool,
    clean_up_replaced: bool,
    dry_run: bool,
    progress: bool | None,
) -> None:
    """Restore from a backup.

    SOURCE is the backup location.
    DESTINATION is where to restore files.
    """
    destination = destination or legacy_destination
    into = into or ("MERGE" if source and not (job or from_path) else "NEW")
    include = _restore_include_path(include)
    if into == "REPLACE" and not yes_replace:
        raise click.UsageError("--yes-replace is required with --into REPLACE")
    if job:
        from backer.core.config import load_config
        from backer.core.paths import get_config_dir

        config = load_config(get_config_dir() / "config.yaml")
        configured = config.jobs.get(job)
        if not configured:
            raise click.ClickException(f"Job '{job}' is not configured")
        record = config.repositories.get(configured.repository)
        if not record:
            raise click.ClickException(f"Job '{job}' names an unknown repository")
        destination = _restore_target(destination, into, Path(configured.source.path))
        _restore_prepare_destination(destination, into, config=config, dry_run=dry_run)
        backend, passphrase, _ = _repository_backend(record)
        source_path = _repository_destination(record)
        snapshots = backend.list_snapshots(BackupDestination(source_path))
        selected, _ = _select_restore_snapshot(snapshots, snapshot, before, latest, computer)
        if into == "REPLACE" and not dry_run:
            _confirm_replace(destination, selected)
        if dry_run:
            click.echo(f"Dry run: would restore immutable snapshot {selected} to {destination}. Nothing was written")
            return
        if into in {"NEW", "MERGE"}:
            destination.mkdir(parents=True, exist_ok=into == "MERGE")
        moved = _replace_destination(destination) if into == "REPLACE" else None
        _restore_execute(
            backend, source_path, destination, selected, include=include, original_source_path=configured.source.path,
            progress=progress, passphrase=passphrase,
        )
        if moved:
            click.echo(f"What was in that folder was moved to {moved}")
            if clean_up_replaced:
                import shutil
                try:
                    shutil.rmtree(moved)
                except OSError as error:
                    click.echo(f"Warning: restored files are safe, but {moved} could not be removed: {error}")
        click.echo("Restore completed")
        return
    if from_path:
        if passphrase_stdin and (password_stdin or secret_key_stdin):
            raise click.UsageError(
                "Repository and storage secret stdin inputs cannot share stdin; "
                "use a file or environment for one secret"
            )
        passphrase_sources = (passphrase_stdin, passphrase_file, os.environ.get("BACKER_REPOSITORY_PASSWORD"))
        if sum(bool(value) for value in passphrase_sources) != 1:
            raise click.UsageError(
                "--from requires exactly one repository passphrase source; it never reads the local keystore"
            )
        raw_passphrase = (
            sys.stdin.read()
            if passphrase_stdin
            else _load_recovery_passphrase(passphrase_file)
            if passphrase_file
            else os.environ.get("BACKER_REPOSITORY_PASSWORD", "")
        )
        passphrase = raw_passphrase.rstrip("\r\n")
        if not passphrase:
            raise click.UsageError("A repository passphrase is required")
        from backer.backends.kopia import KopiaBackend

        cleanup = None
        if from_path.startswith("\\\\") or from_path.startswith("//"):
            parts = from_path.replace("/", "\\").lstrip("\\").split("\\")
            if len(parts) < 2:
                raise click.UsageError("--from SMB path must name a server and share")
            raw_storage_password = (
                sys.stdin.read()
                if password_stdin
                else password_file.read_text()
                if password_file
                else os.environ.get("BACKER_SMB_PASSWORD", "")
            )
            storage_password = raw_storage_password.rstrip("\r\n")
            if not username or not storage_password:
                raise click.UsageError("SMB --from requires --username and a file-server password source")
            from backer.core.mounts import SMBConnectionManager
            manager = SMBConnectionManager()
            if not manager.connect_serverless(parts[0], parts[1], username, storage_password, domain=domain):
                raise click.ClickException("Could not connect to the SMB repository; nothing was changed")
            if manager.serverless_session_created:
                def cleanup() -> None:
                    manager.disconnect_serverless(parts[0], parts[1])
        if from_path.startswith("s3://"):
            from backer.backends.s3 import parse_s3_config

            if secret_key_stdin and secret_key_file:
                raise click.UsageError("Choose at most one of --secret-key-stdin or --secret-key-file")
            secret_key = (
                sys.stdin.read().rstrip("\r\n")
                if secret_key_stdin
                else secret_key_file.read_text(encoding="utf-8").rstrip("\r\n")
                if secret_key_file
                else os.environ.get("BACKER_S3_SECRET_KEY", "")
            )
            path_bucket, _, path_prefix = from_path[5:].partition("/")
            try:
                s3 = parse_s3_config(
                    {
                        "bucket": bucket or path_bucket,
                        "prefix": prefix if prefix is not None else path_prefix,
                        "endpoint": endpoint,
                        "region": region,
                        "access_key_id": access_key_id,
                        "secret_access_key": secret_key,
                    }
                )
            except ValueError as error:
                raise click.UsageError(str(error)) from error
            if bucket and bucket != path_bucket:
                raise click.UsageError("--bucket must match the bucket in --from")
            backend = KopiaBackend({"repository_password": passphrase, "s3": s3.__dict__})
        else:
            backend = KopiaBackend({"repository_password": passphrase})
        try:
            probe_status, _ = backend.repository_probe(from_path)
            rows = backend.list_snapshots(BackupDestination(from_path))
            if probe_status != "present":
                message = backend.last_repository_error or f"Repository is {probe_status}"
                raise click.ClickException(
                    _redact_error(RuntimeError(message), passphrase) + "; nothing was created"
                )
            selected, selected_row = _select_restore_snapshot(rows, snapshot, before, latest, computer)
            original_path = selected_row.get("paths", [None])[0]
            destination = _restore_target(destination, into, Path(original_path) if original_path else None)
            empty_config = type("RestoreConfig", (), {"repositories": {}})()
            repository_paths = () if from_path.startswith(("\\\\", "//", "s3://")) else (Path(from_path),)
            _restore_prepare_destination(
                destination, into, config=empty_config, dry_run=dry_run, repository_paths=repository_paths
            )
            if into == "REPLACE" and not dry_run:
                _confirm_replace(destination, selected)
            if dry_run:
                click.echo(
                    f"Dry run: would restore immutable snapshot {selected} to {destination}. Nothing was written"
                )
                return
            location = destination.parent if destination.parent.exists() else Path.cwd()
            free = __import__("shutil").disk_usage(location).free
            required = next((item.get("size", 0) for item in rows if item.get("full_id") == selected), 0)
            if required and free < required:
                raise click.ClickException("The destination does not have enough free space; nothing was changed")
            if into in {"NEW", "MERGE"}:
                destination.mkdir(parents=True, exist_ok=into == "MERGE")
            moved = _replace_destination(destination) if into == "REPLACE" else None
            _restore_execute(
                backend, from_path, destination, selected, include=include, original_source_path=original_path,
                progress=progress, passphrase=passphrase,
            )
            if moved:
                click.echo(f"What was in that folder was moved to {moved}")
            click.echo("Restore completed")
            click.echo("To keep using this repository: backer repo add NAME --attach, then backer job create")
            return
        finally:
            if cleanup:
                cleanup()
    if source:
        raise click.UsageError(
            "Positional restore was removed; use restore --from PATH with an immutable snapshot selector"
        )
    raise click.UsageError("Choose --job NAME or --from PATH")


# ============ Server commands ============


@main.group()
def server() -> None:
    """Server management commands."""
    pass


def _check_server_dependencies() -> None:
    """Check if all server dependencies are installed.

    Raises ImportError with helpful message if dependencies are missing.
    """
    required_modules = {
        "fastapi": "FastAPI web framework",
        "uvicorn": "ASGI server",
        "jwt": "PyJWT - JWT token support",
        "requests": "HTTP client library",
        "tenacity": "Retry library",
    }

    missing_modules = []
    for module_name, description in required_modules.items():
        try:
            __import__(module_name)
        except ImportError:
            missing_modules.append(f"{module_name} ({description})")

    if missing_modules:
        missing_list = ", ".join(missing_modules)
        raise ImportError(f"Server dependencies missing: {missing_list}. Install with: pip install 'backer[server]'")


@server.command("start")
@click.option("--host", "-h", default="0.0.0.0", help="Host to bind to")
@click.option("--port", "-p", default=8420, help="Port to listen on")
@click.option("--data-dir", type=click.Path(path_type=Path), help="Data directory")
def server_start(host: str, port: int, data_dir: Path | None) -> None:
    """Start the Backer server."""
    console.print(f"[bold]Starting Backer server[/bold] on {host}:{port}")

    try:
        # Check dependencies before importing server modules
        _check_server_dependencies()

        # Import and run the server daemon
        from backer.server.daemon import run_server

        run_server(host=host, port=port, data_dir=data_dir)
    except ImportError as e:
        console.print(f"[red]Error:[/red] Server dependencies not installed: {e}")
        console.print("[yellow]Install with:[/yellow] pip install 'backer[server]'")
        raise SystemExit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] Failed to start server: {e}")
        raise SystemExit(1)


@server.command("update")
@click.option("--dev", is_flag=True, help="Update to the latest dev branch instead of release")
@click.option("--version", "-v", help="Update to a specific version (e.g., 0.4.1)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def server_update(dev: bool, version: str | None, yes: bool) -> None:
    """Update the Backer server to the latest version.

    Downloads and installs the latest release from GitHub, preserving all
    configuration and backup data.

    Examples:
        sudo backer server update           # Update to latest stable release
        sudo backer server update --dev     # Update to latest dev branch
        sudo backer server update -v 0.5.0  # Update to specific version
    """
    import os
    import subprocess
    import sys

    if sys.platform == "win32":
        console.print("[red]Error:[/red] Server update is only supported on Linux")
        raise SystemExit(1)

    # Check if running as root
    if os.geteuid() != 0:
        console.print("[red]Error:[/red] Server update requires root privileges")
        console.print("Run with: sudo backer server update")
        raise SystemExit(1)

    # Determine what we're updating to
    if version:
        target = f"v{version}" if not version.startswith("v") else version
        source_desc = f"version {target}"
        pip_spec = f"git+https://git.stockhome.com.au/stocky789/backer.git@{target}"
    elif dev:
        target = "dev"
        source_desc = "dev branch (latest)"
        pip_spec = "git+https://git.stockhome.com.au/stocky789/backer.git@dev"
    else:
        target = "latest"
        source_desc = "latest stable release"
        pip_spec = "git+https://git.stockhome.com.au/stocky789/backer.git@main"

    console.print("[bold]Backer Server Update[/bold]\n")
    console.print(f"Current version: {__version__}")
    console.print(f"Updating to: {source_desc}")
    console.print()
    console.print("[dim]This will:[/dim]")
    console.print("  • Stop the backer service")
    console.print("  • Update the Python package")
    console.print("  • Restart the backer service")
    console.print("  • [green]Preserve all data in /var/lib/backer[/green]")
    console.print()

    if not yes:
        if not click.confirm("Continue with update?"):
            console.print("Aborted.")
            raise SystemExit(0)

    def run_cmd(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
        """Run a command with error handling."""
        try:
            result = subprocess.run(
                cmd,
                capture_output=capture,
                text=True,
                check=check,
            )
            return result
        except subprocess.CalledProcessError as e:
            if capture:
                console.print(f"[red]Command failed:[/red] {' '.join(cmd)}")
                if e.stdout:
                    console.print(f"[dim]{e.stdout}[/dim]")
                if e.stderr:
                    console.print(f"[red]{e.stderr}[/red]")
            raise

    # Step 1: Stop service
    console.print("\n[bold]1. Stopping service...[/bold]")
    try:
        run_cmd(["systemctl", "stop", "backer"], check=False)
        console.print("   [green]✓[/green] Service stopped")
    except Exception:
        console.print("   [dim]Service not running or not found[/dim]")

    # Step 2: Update the package
    console.print("\n[bold]2. Updating package...[/bold]")

    # Find the venv pip
    venv_pip = Path("/opt/backer/venv/bin/pip")
    if not venv_pip.exists():
        console.print("   [red]Error:[/red] Virtual environment not found at /opt/backer/venv")
        console.print("   Is Backer installed via the install script?")
        raise SystemExit(1)

    try:
        console.print(f"   Installing from {source_desc}...")
        result = run_cmd(
            [str(venv_pip), "install", "--upgrade", pip_spec],
            capture=True,
        )
        console.print("   [green]✓[/green] Package updated")
    except subprocess.CalledProcessError:
        console.print("   [red]✗[/red] Failed to update package")
        # Try to restart service anyway
        run_cmd(["systemctl", "start", "backer"], check=False)
        raise SystemExit(1)

    # Step 3: Get new version
    try:
        result = run_cmd(
            [str(venv_pip.parent / "python"), "-c", "from backer import __version__; print(__version__)"],
            capture=True,
        )
        new_version = result.stdout.strip()
    except Exception:
        new_version = "unknown"

    # Step 4: Restart service
    console.print("\n[bold]3. Restarting service...[/bold]")
    try:
        run_cmd(["systemctl", "start", "backer"])
        console.print("   [green]✓[/green] Service started")
    except subprocess.CalledProcessError:
        console.print("   [red]✗[/red] Failed to start service")
        console.print("   Check logs with: journalctl -u backer -n 50")
        raise SystemExit(1)

    # Step 5: Verify service is running
    console.print("\n[bold]4. Verifying...[/bold]")
    import time

    time.sleep(2)  # Give service time to start

    try:
        result = run_cmd(["systemctl", "is-active", "backer"], check=False, capture=True)
        if result.returncode == 0:
            console.print("   [green]✓[/green] Service is running")
        else:
            console.print("   [yellow]Warning:[/yellow] Service may not be running properly")
            console.print("   Check logs with: journalctl -u backer -n 50")
    except Exception:
        pass

    console.print("\n[bold green]✓ Update complete[/bold green]")
    console.print(f"  Previous version: {__version__}")
    console.print(f"  New version: {new_version}")
    console.print()
    console.print("[dim]Your data in /var/lib/backer has been preserved.[/dim]")


@server.command("uninstall")
@click.option("--keep-data", is_flag=True, help="Keep backup data in /var/lib/backer")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def server_uninstall(keep_data: bool, yes: bool) -> None:
    """Uninstall Backer server from this system.

    This will:
    - Stop and disable the backer systemd service
    - Remove the service file
    - Remove /opt/backer and /usr/local/bin/backer
    - Optionally remove /var/lib/backer (backup data)
    - Remove the backer system user

    Use --keep-data to preserve your backup configuration and history.
    """
    import os
    import subprocess
    import sys

    if sys.platform == "win32":
        console.print("[red]Error:[/red] Server uninstall is only supported on Linux")
        console.print("On Windows, use the Control Panel to uninstall Backer")
        raise SystemExit(1)

    # Check if running as root
    if os.geteuid() != 0:
        console.print("[red]Error:[/red] Server uninstall requires root privileges")
        console.print("Run with: sudo backer server uninstall")
        raise SystemExit(1)

    console.print("[bold]Backer Server Uninstall[/bold]\n")
    console.print("This will remove:")
    console.print("  • Systemd service (backer.service)")
    console.print("  • Installation directory (/opt/backer)")
    console.print("  • Binary symlink (/usr/local/bin/backer)")
    if not keep_data:
        console.print("  • [yellow]Backup data and config (/var/lib/backer)[/yellow]")
        console.print("  • [yellow]User data (~/.local/share/backer)[/yellow]")
    else:
        console.print("  • [dim]Backup data will be preserved[/dim]")
    console.print("  • System user (backer)")
    console.print()

    if not yes:
        if not click.confirm("Continue with uninstall?"):
            console.print("Aborted.")
            raise SystemExit(0)

    def run_cmd(cmd: list[str], ignore_errors: bool = False) -> bool:
        """Run a command and return success status."""
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            return result.returncode == 0
        except Exception:
            return ignore_errors

    # Stop and disable service
    console.print("\n[bold]1. Stopping service...[/bold]")
    if run_cmd(["systemctl", "stop", "backer"]):
        console.print("   [green]✓[/green] Service stopped")
    else:
        console.print("   [dim]Service not running or not found[/dim]")

    if run_cmd(["systemctl", "disable", "backer"]):
        console.print("   [green]✓[/green] Service disabled")
    else:
        console.print("   [dim]Service not enabled or not found[/dim]")

    # Remove service file
    console.print("\n[bold]2. Removing service file...[/bold]")
    service_file = Path("/etc/systemd/system/backer.service")
    if service_file.exists():
        service_file.unlink()
        console.print("   [green]✓[/green] Removed /etc/systemd/system/backer.service")
        run_cmd(["systemctl", "daemon-reload"])
        console.print("   [green]✓[/green] Reloaded systemd")
    else:
        console.print("   [dim]Service file not found[/dim]")

    # Remove installation directories
    console.print("\n[bold]3. Removing installation files...[/bold]")
    import shutil

    dirs_to_remove = ["/opt/backer"]
    if not keep_data:
        dirs_to_remove.append("/var/lib/backer")
        # Also check for user data directories
        # When running as sudo, SUDO_USER contains the original user
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user:
            user_data_dir = Path(f"/home/{sudo_user}/.local/share/backer")
            if user_data_dir.exists():
                dirs_to_remove.append(str(user_data_dir))

    for dir_path in dirs_to_remove:
        path = Path(dir_path)
        if path.exists():
            shutil.rmtree(path)
            console.print(f"   [green]✓[/green] Removed {dir_path}")
        else:
            console.print(f"   [dim]{dir_path} not found[/dim]")

    # Remove symlink
    symlink = Path("/usr/local/bin/backer")
    if symlink.exists() or symlink.is_symlink():
        symlink.unlink()
        console.print("   [green]✓[/green] Removed /usr/local/bin/backer")
    else:
        console.print("   [dim]/usr/local/bin/backer not found[/dim]")

    # Remove user
    console.print("\n[bold]4. Removing system user...[/bold]")
    if run_cmd(["userdel", "backer"], ignore_errors=True):
        console.print("   [green]✓[/green] Removed backer user")
    else:
        console.print("   [dim]User not found or could not be removed[/dim]")

    console.print("\n[bold green]✓ Backer server uninstalled successfully[/bold green]")

    if keep_data:
        console.print("\n[yellow]Note:[/yellow] Backup data preserved in /var/lib/backer")
        console.print("To remove it later: sudo rm -rf /var/lib/backer")

    # Suggest cleaning up user files
    console.print("\n[dim]To clean up user files, run as your user:[/dim]")
    console.print("[dim]  rm -rf ~/backer ~/venv ~/.local/share/backer[/dim]")


# ============ Agent commands ============


@main.group()
def agent() -> None:
    """Agent management commands."""
    pass


@main.group()
def repo() -> None:
    """Manage local serverless repositories."""
    pass


def _read_passphrase(stream: bool, file: Path | None) -> str:
    if stream == (file is not None):
        if stream:
            raise click.UsageError("Choose exactly one of --passphrase-stdin or --passphrase-file")
        value = os.environ.get("BACKER_REPOSITORY_PASSWORD", "")
    else:
        value = sys.stdin.read() if stream else _load_recovery_passphrase(file)
    value = value.rstrip("\r\n")
    if not value:
        raise click.UsageError("Repository passphrase is required")
    return value


def _read_storage(stream: bool, file: Path | None) -> str | None:
    if not stream and file is None:
        return None
    if stream and file is not None:
        raise click.UsageError("Choose at most one of --storage-stdin or --storage-file")
    return (sys.stdin.read() if stream else file.read_text(encoding="utf-8")).rstrip("\r\n")


@repo.command("add")
@click.argument("name")
@click.option("--attach", is_flag=True, help="Attach an existing repository")
@click.option("--init", "initialize", is_flag=True, help="Create a new repository")
@click.option("--type", "repository_type", type=click.Choice(["local", "smb", "s3"]), default="local")
@click.option("--path")
@click.option("--server", "--host", "server")
@click.option("--share")
@click.option("--username")
@click.option("--domain")
@click.option("--bucket")
@click.option("--prefix")
@click.option("--endpoint")
@click.option("--region")
@click.option("--path-style", is_flag=True)
@click.option(
    "--storage-stdin",
    "--password-stdin",
    "storage_stdin",
    is_flag=True,
    help="Read SMB password or S3 credentials JSON from stdin",
)
@click.option("--storage-file", "--password-file", type=click.Path(exists=True, path_type=Path))
@click.option("--access-key-id")
@click.option("--secret-key-stdin", is_flag=True, help="Read the S3 secret key from stdin")
@click.option("--secret-key-file", type=click.Path(exists=True, path_type=Path))
@click.option("--adopt", is_flag=True, help="Attach an existing repository for sidecar adoption")
@click.option("--passphrase-stdin", is_flag=True)
@click.option("--passphrase-file", type=click.Path(exists=True, path_type=Path))
@click.option("--generate-passphrase", is_flag=True)
@click.option("--passphrase-out", type=click.Path(path_type=Path))
@click.option("--print-passphrase", is_flag=True)
@click.option("--update-password", is_flag=True)
@click.option("--update-passphrase", is_flag=True)
@click.option("--headless", is_flag=True, help="Allow the protected local-file secret fallback")
@click.option("--yes", is_flag=True)
@click.pass_context
def repo_add(
    ctx: click.Context,
    name: str,
    attach: bool,
    initialize: bool,
    repository_type: str,
    path: str | None,
    server: str | None,
    share: str | None,
    username: str | None,
    domain: str | None,
    bucket: str | None,
    prefix: str | None,
    endpoint: str | None,
    region: str | None,
    path_style: bool,
    storage_stdin: bool,
    storage_file: Path | None,
    access_key_id: str | None,
    secret_key_stdin: bool,
    secret_key_file: Path | None,
    adopt: bool,
    passphrase_stdin: bool,
    passphrase_file: Path | None,
    generate_passphrase: bool,
    passphrase_out: Path | None,
    print_passphrase: bool,
    update_password: bool,
    update_passphrase: bool,
    headless: bool,
    yes: bool,
    storage_password: str | None = None,
    generated_passphrase: str | None = None,
) -> None:
    """Attach or explicitly create one repository."""
    from backer.core.config import BackerConfig, RepositoryConfig, load_config
    from backer.core.paths import get_config_dir
    from backer.serverless.repositories import add_repository

    try:
        if passphrase_stdin and (storage_stdin or secret_key_stdin):
            raise click.UsageError("Use --passphrase-file when --storage-stdin is used")
        if generate_passphrase:
            if passphrase_stdin or passphrase_file:
                raise click.UsageError("Choose one passphrase source")
            if not _interactive() and not (passphrase_out or print_passphrase):
                _missing_flags("repo add " + name, ["--passphrase-out FILE or --print-passphrase"])
            passphrase = generated_passphrase or _generated_passphrase()
            if print_passphrase and generated_passphrase is None:
                console.print(passphrase)
        else:
            passphrase = _read_passphrase(passphrase_stdin, passphrase_file)
        raw_storage = storage_password if storage_password is not None else _read_storage(storage_stdin, storage_file)
        if raw_storage is None and repository_type == "smb":
            raw_storage = os.environ.get("BACKER_SMB_PASSWORD")
        if raw_storage is None and repository_type == "s3":
            secret = os.environ.get("BACKER_S3_SECRET_KEY")
            if secret:
                raw_storage = json.dumps({"access_key_id": access_key_id or "", "secret_access_key": secret})
        if secret_key_stdin and secret_key_file:
            raise click.UsageError("Choose at most one of --secret-key-stdin or --secret-key-file")
        secret_key = (
            (sys.stdin.read() if secret_key_stdin else secret_key_file.read_text(encoding="utf-8"))
            if (secret_key_stdin or secret_key_file)
            else None
        )
        if repository_type == "s3" and secret_key:
            raw_storage = json.dumps(
                {"access_key_id": access_key_id or "", "secret_access_key": secret_key.rstrip("\r\n")}
            )
        storage = json.loads(raw_storage) if repository_type == "s3" and raw_storage else raw_storage
        config_path = ctx.obj.get("config_path") or get_config_dir() / "config.yaml"
        config = load_config(config_path) if config_path.exists() else BackerConfig()
        if repository_type == "local" and not path:
            raise click.UsageError("--path is required for local repositories")
        if repository_type == "smb":
            _missing_flags(
                "repo add " + name,
                [
                    flag
                    for flag, value in (
                        ("--host", server),
                        ("--share", share),
                        ("--path", path),
                        ("--username", username),
                        ("--password-stdin or --password-file", raw_storage),
                    )
                    if not value
                ],
            )
        if repository_type == "s3":
            _missing_flags(
                "repo add " + name,
                [
                    flag
                    for flag, value in (
                        ("--bucket", bucket),
                        ("--endpoint", endpoint),
                        ("--region", region),
                        (
                            "--access-key-id",
                            access_key_id or (storage or {}).get("access_key_id")
                            if isinstance(storage, dict)
                            else None,
                        ),
                        ("--secret-key-stdin or --secret-key-file", storage),
                    )
                    if not value
                ],
            )
        record = RepositoryConfig(
            id=uuid4().hex[:12],
            name=name,
            type=repository_type,
            path=path,
            server=server,
            share=share,
            username=username,
            domain=domain,
            bucket=bucket,
            prefix=prefix,
            endpoint=endpoint,
            region=region,
            path_style=path_style or None,
        )
        if passphrase_out:
            _write_recovery_export(passphrase_out, name, record.id, _repository_destination(record), passphrase)
        repo_id, backend = add_repository(
            config,
            config_path,
            name,
            record,
            passphrase,
            attach=attach,
            init=initialize,
            storage=storage,
            headless=headless,
            adopt=adopt,
        )
        console.print(f"Repository '{name}' saved ({backend}, id {repo_id})")
        if backend == "file":
            console.print("Warning: secrets are stored in protected local files")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise click.ClickException(str(error)) from error


@repo.command("adopt")
@click.argument("name")
@click.option("--job", "jobs", multiple=True, help="Sidecar job name to import (repeatable)")
@click.option("--all", "all_jobs", is_flag=True, help="Import every discovered sidecar job")
@click.option("--source", "sources", multiple=True, help="NAME=local source path")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def repo_adopt(
    ctx: click.Context, name: str, jobs: tuple[str, ...], all_jobs: bool, sources: tuple[str, ...], as_json: bool
) -> None:
    """Copy existing repository sidecar jobs into this machine's config."""
    import json

    from backer.core import keystore
    from backer.core.config import load_config
    from backer.core.paths import get_config_dir
    from backer.core.repo_metadata import RepositoryMetadata
    from backer.serverless.repositories import _destination, probe
    from backer.serverless.sidecar import adopt_documents, adopt_jobs

    config_path = ctx.obj.get("config_path") or get_config_dir() / "config.yaml"
    config = load_config(config_path)
    repository_id = next((key for key, value in config.repositories.items() if key == name or value.name == name), None)
    if not repository_id:
        raise click.ClickException(f"Repository '{name}' is not configured")
    record = config.repositories[repository_id]
    machine_scope = record.scope == "machine"
    passphrase = keystore.get(record.passphrase_ref or "", machine_scope=machine_scope)
    if not passphrase:
        raise click.ClickException(f"Repository '{record.name}' passphrase is unavailable")
    raw_storage = keystore.get(record.storage_password_ref or "", machine_scope=machine_scope)
    storage = json.loads(raw_storage) if record.type == "s3" and raw_storage else None
    status, _, message = probe(record, passphrase, storage)
    if status != "present":
        raise click.ClickException(message or f"Repository is {status}; adoption did not change local config")
    smb_manager = None
    smb_created = False
    if record.type == "smb":
        from backer.core.mounts import SMBConnectionManager

        smb_manager = SMBConnectionManager()
        smb_created = smb_manager._find_existing_connection(record.server or "") is None
        if not smb_manager.connect_serverless(
            record.server or "", record.share or "", record.username or "", raw_storage or "", domain=record.domain
        ):
            raise click.ClickException("Could not connect to SMB repository for adoption")
    root = Path(_destination(record))
    documents = None
    if record.type == "s3":
        from backer.serverless.s3_sidecar import S3Sidecar

        if not storage:
            raise click.ClickException(f"Repository '{record.name}' storage credential is unavailable")
        sidecar = S3Sidecar(record.model_dump(exclude_none=True), storage)
        documents = {}
        for key in sidecar.list(".backer/jobs/"):
            if key.endswith("/config.json"):
                document = sidecar.get(key.removeprefix(f"{record.prefix.strip('/')}/"))
                if document:
                    payload = json.loads(document)
                    documents[payload["job_name"]] = payload
        available = list(documents)
    else:
        discovery = RepositoryMetadata(root, record.type).discover_all()
        available = [item.get("job_name") for item in discovery["jobs"] if item.get("job_name")]
    selected = available if all_jobs else list(jobs)
    if not selected:
        raise click.ClickException("Choose --job NAME or --all")
    mapping = dict(item.split("=", 1) for item in sources if "=" in item)
    try:
        adopted = (
            adopt_documents(config, repository_id, documents, selected, source_paths=mapping)
            if documents is not None
            else adopt_jobs(config, repository_id, root, selected, source_paths=mapping)
        )
        config.save(config_path)
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    finally:
        if smb_manager and smb_created:
            smb_manager.disconnect_serverless(record.server or "", record.share or "")
    if as_json:
        click.echo(json.dumps(adopted))
    else:
        for job_name in adopted:
            console.print(f"Adopted {job_name}")


def _local_config(ctx: click.Context):
    from backer.core.config import load_config
    from backer.core.paths import get_config_dir

    path = ctx.obj.get("config_path") or get_config_dir() / "config.yaml"
    return path, load_config(path)


def _repository(config, name: str):
    item = next(((key, value) for key, value in config.repositories.items() if key == name or value.name == name), None)
    if not item:
        raise click.ClickException(f"Repository '{name}' is not configured")
    return item


def _repository_backend(record, *, timeout: int | None = None):
    """Open one configured repository without ever placing a secret in argv."""
    from backer.core import keystore
    from backer.serverless.repositories import _backend

    passphrase = keystore.get(record.passphrase_ref or "", machine_scope=record.scope == "machine")
    if not passphrase:
        raise click.ClickException("Repository passphrase is unavailable")
    storage = None
    if record.storage_password_ref:
        raw = keystore.get(record.storage_password_ref, machine_scope=record.scope == "machine")
        if not raw:
            raise click.ClickException("Repository storage credential is unavailable")
        storage = json.loads(raw) if record.type == "s3" else raw
    backend = _backend(record, passphrase, storage)
    if timeout:
        backend.config["timeout"] = timeout
    return backend, passphrase, storage


def _repository_destination(record) -> str:
    from backer.serverless.repositories import _destination

    return _destination(record)


def _restore_destination_allowed(destination: Path, config, repository_paths: tuple[Path, ...] = ()) -> None:
    resolved = destination.expanduser().resolve()
    system_roots = ({Path(os.environ.get("WINDIR", "C:/Windows")).resolve(),
                     Path(os.environ.get("ProgramFiles", "C:/Program Files")).resolve()}
                    if os.name == "nt" else {Path("/").resolve(), Path("/home").resolve(), Path("/usr").resolve()})
    if resolved == Path.home().resolve():
        raise click.ClickException(
            f"Backer will not restore over {Path.home().resolve()}. Choose a folder inside it instead."
        )
    for record in config.repositories.values():
        if record.type == "local" and record.path:
            denied = Path(record.path).expanduser().resolve()
            if resolved == denied or denied in resolved.parents or resolved in denied.parents:
                raise click.ClickException(f"Backer will not restore over {denied}. Choose a folder inside it instead.")
    for denied in repository_paths:
        denied = denied.expanduser().resolve()
        if resolved == denied or denied in resolved.parents or resolved in denied.parents:
            raise click.ClickException(f"Backer will not restore over {denied}. Choose a folder inside it instead.")
    for denied in system_roots:
        if resolved == denied or denied in resolved.parents or resolved in denied.parents:
            raise click.ClickException(f"Backer will not restore over {denied}. Choose a folder inside it instead.")


def _restore_target(destination: Path | None, into: str, original_source: Path | None = None) -> Path:
    """Choose NEW's unique sibling without ever reusing a real directory."""
    if destination is not None:
        return destination.expanduser()
    if into != "NEW" or original_source is None:
        raise click.UsageError("--destination is required unless --into NEW restores a known job")
    stamp = datetime.now().strftime("%Y-%m-%d")
    candidate = original_source.with_name(f"{original_source.name} (restored {stamp})")
    suffix = 2
    while candidate.exists():
        candidate = original_source.with_name(f"{original_source.name} (restored {stamp})-{suffix}")
        suffix += 1
    return candidate


def _restore_prepare_destination(
    destination: Path, into: str, *, config, dry_run: bool = False, repository_paths: tuple[Path, ...] = ()
) -> Path | None:
    """Perform every non-destructive destination refusal before a restore starts."""
    destination = destination.expanduser()
    _restore_destination_allowed(destination, config, repository_paths)
    if into == "NEW":
        if destination.exists():
            raise click.ClickException("A NEW restore destination must not exist; nothing was changed")
        return None
    if into == "MERGE":
        return None
    if into != "REPLACE":
        raise click.UsageError("--into must be NEW, MERGE, or REPLACE")
    if not destination.exists():
        raise click.ClickException("REPLACE requires an existing destination; nothing was changed")
    if destination.is_symlink() or not destination.is_dir():
        raise click.ClickException("Restore destination must be a non-symlink directory")
    return destination


def _confirm_replace(destination: Path, selected: str) -> None:
    """REPLACE stays interactive even when callers supplied convenience flags."""
    if not _interactive():
        raise click.UsageError("REPLACE requires an interactive typed confirmation")
    count = sum(1 for item in destination.rglob("*") if item.is_file())
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    click.echo(
        f"{destination} holds {count} files. They will move to {destination.name}.replaced-{stamp} "
        f"before snapshot {selected} is restored"
    )
    if click.prompt("Type REPLACE to continue", default="", show_default=False) != "REPLACE":
        raise click.ClickException("Stopped. Nothing was changed")


def _replace_destination(destination: Path) -> Path | None:
    if not destination.exists():
        destination.mkdir(parents=True, exist_ok=True)
        return None
    if destination.is_symlink() or not destination.is_dir():
        raise click.ClickException("Restore destination must be a non-symlink directory")
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    moved = destination.with_name(f"{destination.name}.replaced-{stamp}")
    suffix = 2
    while moved.exists():
        moved = destination.with_name(f"{destination.name}.replaced-{stamp}-{suffix}")
        suffix += 1
    destination.replace(moved)
    destination.mkdir()
    return moved


class _LocalProgress:
    """Render only real Kopia frames; first backups intentionally stay indeterminate."""

    def __init__(self, enabled: bool | None) -> None:
        self.live_mode = sys.stdout.isatty() if enabled is None else enabled
        self.progress = None
        self.live = None
        self.task = None
        self.last_frame = time.monotonic()
        self.stall_timer: threading.Timer | None = None

    def __enter__(self):
        if self.live_mode:
            from rich.live import Live
            from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

            self.progress = Progress(
                SpinnerColumn(), TextColumn("[progress.description]{task.description}"), BarColumn()
            )
            self.task = self.progress.add_task("Waiting for Kopia progress", total=None)
            self.live = Live(self.progress, console=console, refresh_per_second=4)
            self.live.__enter__()
            self.stall_timer = threading.Timer(45, self._stalled)
            self.stall_timer.daemon = True
            self.stall_timer.start()
        return self.callback

    def _stalled(self) -> None:
        if self.live and time.monotonic() - self.last_frame >= 45:
            self.progress.update(self.task, description="no progress for 45s")

    def callback(self, **event: Any) -> None:
        if "bytes_processed" not in event:
            return
        done, total = event["bytes_processed"], event.get("total_bytes", 0)
        self.last_frame = time.monotonic()
        if not self.live_mode:
            stamp = datetime.now().strftime("%H:%M:%S")
            click.echo(f"{stamp} {done}/{total} bytes" if total else f"{stamp} {done} bytes")
            return
        if total:
            self.progress.update(
                self.task, completed=min(done, total), total=total, description=f"{done}/{total} bytes"
            )
        else:
            self.progress.update(self.task, total=None, description=f"{done} bytes")

    def __exit__(self, *_args) -> None:
        if self.stall_timer:
            self.stall_timer.cancel()
        if self.live:
            self.live.__exit__(*_args)


def _local_progress(enabled: bool | None) -> _LocalProgress:
    return _LocalProgress(enabled)


def _resolve_job_repository(config, requested: str | None, name: str | None = None) -> str:
    if requested:
        repository_id, _ = _repository(config, requested)
        if name and name in config.jobs and config.jobs[name].repository != repository_id:
            raise click.UsageError(f"Job '{name}' does not belong to repository '{requested}'")
        return repository_id
    if name and name in config.jobs:
        return config.jobs[name].repository
    if len(config.repositories) == 1:
        return next(iter(config.repositories))
    raise click.UsageError("--repo is required when more than one local repository is configured")


@repo.command("list")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def repo_list(ctx: click.Context, as_json: bool) -> None:
    """List local repository records."""
    _, config = _local_config(ctx)
    rows = [
        {"id": key, **value.model_dump(exclude={"passphrase_ref", "storage_password_ref"})}
        for key, value in config.repositories.items()
    ]
    if as_json:
        click.echo(json.dumps(rows))
    else:
        for row in rows:
            click.echo(f"{row['id']}\t{row['name']}\t{row['type']}")


@repo.command("test")
@click.argument("name")
@click.pass_context
def repo_test(ctx: click.Context, name: str) -> None:
    """Probe a repository without creating it."""
    from backer.core import keystore
    from backer.serverless.repositories import probe

    _, config = _local_config(ctx)
    _, record = _repository(config, name)
    passphrase = keystore.get(record.passphrase_ref or "", machine_scope=record.scope == "machine")
    if not passphrase:
        raise click.ClickException("Repository passphrase is unavailable")
    status, _, message = probe(record, passphrase)
    if status != "present":
        raise click.ClickException(message or f"Repository is {status}")
    click.echo("Repository connection succeeded")


@repo.command("unlock")
@click.argument("name")
@click.pass_context
def repo_unlock(ctx: click.Context, name: str) -> None:
    """Discard this machine's isolated connection state, then probe it."""
    # The per-repository config is deliberately disposable; Kopia reconnects next operation.
    _, config = _local_config(ctx)
    _, record = _repository(config, name)
    backend, _, _ = _repository_backend(record)
    backend._disconnect_repo(_repository_destination(record))
    click.echo(f"Repository '{name}' disconnected and will reconnect on its next operation")


@repo.command("passphrase")
@click.argument("name")
@click.option("--passphrase-out", type=click.Path(path_type=Path))
@click.option("--print-passphrase", is_flag=True)
@click.pass_context
def repo_passphrase(ctx: click.Context, name: str, passphrase_out: Path | None, print_passphrase: bool) -> None:
    """Export the stored repository passphrase only to an explicit destination."""
    from backer.core import keystore

    _, config = _local_config(ctx)
    _, record = _repository(config, name)
    value = keystore.get(record.passphrase_ref or "", machine_scope=record.scope == "machine")
    if not value:
        raise click.ClickException("Repository passphrase is unavailable")
    if not passphrase_out and not print_passphrase:
        _missing_flags("repo passphrase " + name, ["--passphrase-out FILE or --print-passphrase"])
    if passphrase_out:
        _write_recovery_export(passphrase_out, record.name, record.id, _repository_destination(record), value)
    if print_passphrase:
        click.echo(value)


@repo.command("rm")
@click.argument("name")
@click.option("--yes", is_flag=True)
@click.option("--passphrase-out", type=click.Path(path_type=Path))
@click.pass_context
def repo_rm(ctx: click.Context, name: str, yes: bool, passphrase_out: Path | None) -> None:
    """Remove local access only; repository data is left untouched."""
    from backer.core import keystore

    path, config = _local_config(ctx)
    key, record = _repository(config, name)
    if not yes:
        raise click.UsageError("--yes acknowledges that backup data will be left unreadable on this computer")
    if not _interactive():
        raise click.UsageError("Repository removal requires an interactive typed repository name")
    if click.prompt("Type the repository name to remove local access") != name:
        raise click.ClickException("Repository name did not match; nothing was changed")
    if not passphrase_out:
        raise click.UsageError("--passphrase-out FILE is required before removing local access")
    value = keystore.get(record.passphrase_ref or "", machine_scope=record.scope == "machine")
    if not value:
        raise click.ClickException("Repository passphrase is unavailable")
    _write_recovery_export(passphrase_out, record.name, record.id, _repository_destination(record), value)
    for reference in (record.passphrase_ref, record.storage_password_ref):
        if reference:
            keystore.delete(reference, machine_scope=record.scope == "machine")
    del config.repositories[key]
    config.save(path)
    click.echo(f"Repository '{name}' removed locally; backup data was not deleted")


@repo.command("discover")
@click.option("--host", required=True)
@click.option("--username", required=True)
@click.option("--password-stdin", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def repo_discover(host: str, username: str, password_stdin: bool, as_json: bool) -> None:
    """Discover shares on one named SMB host; never scan a network."""
    password = sys.stdin.read().rstrip("\r\n") if password_stdin else os.environ.get("BACKER_SMB_PASSWORD", "")
    if not password:
        _missing_flags("repo discover", ["--password-stdin"])
    from backer.core.smb_browse import SMBBrowser

    success, result = SMBBrowser.list_shares(host, username, password)
    if not success:
        raise click.ClickException(str(result))
    rows = [{"name": share.name, "comment": share.comment} for share in result]
    if as_json:
        click.echo(json.dumps(rows))
    else:
        for row in rows:
            click.echo(f"{row['name']}\t{row['comment']}")


@repo.command("recover")
@click.argument("name")
@click.option("--passphrase-stdin", is_flag=True)
@click.option("--passphrase-file", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def repo_recover(ctx: click.Context, name: str, passphrase_stdin: bool, passphrase_file: Path | None) -> None:
    """Explicitly store a passphrase only after it opens the existing repository."""
    from backer.core import keystore
    from backer.serverless.repositories import probe

    if bool(passphrase_stdin) == bool(passphrase_file):
        raise click.UsageError("Choose exactly one of --passphrase-stdin or --passphrase-file")
    path, config = _local_config(ctx)
    key, record = _repository(config, name)
    passphrase = (
        sys.stdin.read().rstrip("\r\n")
        if passphrase_stdin
        else _load_recovery_passphrase(passphrase_file)
    )
    if not passphrase:
        raise click.UsageError("A repository passphrase is required")
    storage = None
    storage_secret = None
    if record.storage_password_ref:
        storage_secret = keystore.get(record.storage_password_ref, machine_scope=record.scope == "machine")
        storage = json.loads(storage_secret) if storage_secret and record.type == "s3" else storage_secret
    status, _, message = probe(record, passphrase, storage)
    if status != "present":
        safe_message = _redact_error(
            RuntimeError(message or "The supplied passphrase could not open this existing repository"),
            passphrase,
            storage_secret,
        )
        raise click.ClickException(safe_message)
    reference = record.passphrase_ref or f"backer/repo/{key}/passphrase"
    keystore.put(reference, passphrase, machine_scope=record.scope == "machine")
    config.repositories[key] = record.model_copy(update={"passphrase_ref": reference})
    config.save(path)
    click.echo(f"Repository '{record.name}' opened. Its passphrase is now stored on this computer")


@agent.command("setup")
def agent_setup() -> None:
    """Run the interactive setup wizard.

    Guides you through connecting to a server and registering this machine.
    Automatically removes any existing configuration to start fresh.
    """
    from backer.client.setup_wizard import run_wizard
    from backer.core.config import load_config
    from backer.core.paths import get_config_dir

    config_dir = get_config_dir()
    config_path = config_dir / "config.yaml"
    if config_path.exists():
        config = load_config(config_path)
        config.server = None
        config.save(config_path)

    success = run_wizard()
    if not success:
        raise SystemExit(1)


@agent.command("register")
@click.option("--server", "-s", required=True, help="Server URL (e.g., http://backup-server:8420)")
@click.option("--token", envvar="BACKER_ENROLLMENT_TOKEN", help="Single-use enrollment token from the Agents page")
def agent_register(server: str, token: str | None) -> None:
    """Register this machine as a backup client (use 'setup' for interactive mode)."""
    from backer.client.agent import BackerAgent

    console.print(f"Registering with server: {server}")

    try:
        ag = BackerAgent(server_url=server)
        client_id, _ = ag.register(token)
        console.print("[green]✓ Registered successfully[/green]")
        console.print(f"  Client ID: {client_id}")
        console.print(f"  Config saved to: {ag.config_path}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)


@agent.command("configure")
@click.option("--server", "-s", required=True, help="Server URL (e.g., http://backup-server:8420)")
@click.option("--client-id", required=True, help="Client ID from server")
@click.option("--client-secret", required=True, help="Client secret from server")
def agent_configure(server: str, client_id: str, client_secret: str) -> None:
    """Manually configure agent with server-provided credentials.

    Use this when re-adding an agent that was previously registered, or when
    credentials need to be reset. Get the credentials from the server web UI
    by clicking "Reset" on the agent.

    Example:
        backer agent configure --server http://backup:8420 --client-id abc123 --client-secret xyz...
    """
    from backer.client.agent import BackerAgent

    console.print(f"Configuring agent for server: {server}")
    console.print(f"  Client ID: {client_id}")

    try:
        # Create agent and save the provided credentials
        ag = BackerAgent(server_url=server)
        ag.client_id = client_id
        ag.client_secret = client_secret
        ag._save_credentials()

        # Verify the credentials work by doing a heartbeat
        console.print("Verifying connection...")
        ag.heartbeat()

        console.print("[green]✓ Configuration saved and verified[/green]")
        console.print(f"  Config saved to: {ag.config_path}")
        console.print()
        console.print("[dim]Restart the agent service to apply changes:[/dim]")
        console.print("  sudo systemctl restart backer-agent")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print()
        console.print("[yellow]The credentials may be invalid.[/yellow]")
        console.print("Try resetting credentials again from the server web UI.")
        raise SystemExit(1)


@agent.command("start")
@click.option("--server", "-s", help="Server URL (uses saved config if not specified)")
@click.option("--token", envvar="BACKER_ENROLLMENT_TOKEN", help="Enrollment token when registering a new agent")
def agent_start(server: str | None, token: str | None) -> None:
    """Start the backup agent."""
    from backer.client.agent import BackerAgent

    try:
        if server:
            try:
                ag = BackerAgent.from_config()
                if ag.server_url != server:
                    console.print("[yellow]Warning:[/yellow] Server URL differs from saved config")
            except FileNotFoundError:
                console.print("No saved config found. Registering with server...")
                ag = BackerAgent(server_url=server)
                ag.register(token)
        else:
            ag = BackerAgent.from_config()

        console.print(f"[bold]Starting agent[/bold] (client_id: {ag.client_id})")
        ag.run()

    except FileNotFoundError:
        console.print("[red]Error:[/red] No agent config found. Run 'backer agent register' first.")
        raise SystemExit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)


@agent.command("status")
def agent_status() -> None:
    """Show agent status."""
    from backer.client.agent import BackerAgent
    from backer.client.windows_service import get_service_status, is_windows

    try:
        ag = BackerAgent.from_config()
        console.print(f"Client ID: {ag.client_id}")
        console.print(f"Server: {ag.server_url}")

        try:
            ag.heartbeat()
            console.print("[green]✓ Connected to server[/green]")
        except Exception as e:
            console.print(f"[yellow]✗ Cannot reach server:[/yellow] {e}")

        # Show service status on Windows
        if is_windows():
            service_info = get_service_status()
            if service_info.get("installed"):
                method = service_info.get("method", "unknown")
                status = service_info.get("status", "Unknown")
                running = service_info.get("running", False)

                if method == "background_task":
                    method_desc = "Background Service"
                elif method == "logon_task":
                    method_desc = "Logon Task"
                elif method == "startup_script":
                    method_desc = "Startup Script"
                else:
                    method_desc = method

                status_color = "green" if running else "yellow"
                console.print(f"Service: [{status_color}]{status}[/{status_color}] ({method_desc})")

                if method == "logon_task":
                    console.print(
                        "[dim]  Note: Logon task stops at lockscreen. "
                        "Use 'backer agent install --method service' for background mode.[/dim]"
                    )
            else:
                console.print("[dim]Service: Not installed (run 'backer agent install')[/dim]")

    except FileNotFoundError:
        console.print("[yellow]Agent not registered[/yellow]")
        console.print("Run 'backer agent register --server <url>' to register")


@agent.command("install")
@click.option("--mode", type=click.Choice(["server", "local"]), default="server", show_default=True)
@click.option("--headless", is_flag=True, help="Install the machine local scheduler")
@click.option(
    "--method",
    "-m",
    default="service",
    type=click.Choice(["service", "task", "startup", "systemd"]),
    help="Installation method (Windows: service/task/startup, Linux: systemd)",
)
def agent_install(mode: str, headless: bool, method: str) -> None:
    """Install agent to run at system startup.

    On Windows, the default method is 'service' which creates a background task
    that runs at boot and continues running even when:
    - No user is logged in
    - User is at the lockscreen
    - User logs off or switches users

    Installation methods:
        service  - (default) Background service, runs at boot (recommended)
        task     - Scheduled task at user logon (legacy)
        startup  - Startup folder script (legacy)
        systemd  - Linux systemd service

    Examples:
        backer agent install                  # Default: background service
        backer agent install --method service # Background service (lockscreen-safe)
        backer agent install --method task    # Legacy: runs at user logon
        backer agent install --method systemd # Linux systemd service
    """
    from backer.client.windows_service import (
        create_local_scheduled_task,
        create_local_systemd_timer,
        create_systemd_service,
        install_service,
        is_admin,
        is_windows,
    )

    if mode == "local":
        if is_windows() and method != "task":
            raise click.UsageError("--mode local on Windows requires --method task")
        if not is_windows() and method != "systemd":
            raise click.UsageError("--mode local on Linux requires --method systemd")
        from backer.core.config import load_config
        from backer.core.paths import get_config_dir, get_machine_config_dir
        from backer.serverless.repositories import rescope_secrets_for_system

        config = load_config(get_config_dir() / "config.yaml")
        try:
            rescope_secrets_for_system(config)
        except ValueError as error:
            raise click.ClickException(str(error)) from error
        machine_config = get_machine_config_dir() / "config.yaml"
        config.save(machine_config)
        if not is_windows():
            success, message = create_local_systemd_timer(headless=headless)
            if not success:
                raise click.ClickException(message)
            console.print(f"[green]✓[/green] {message} (backer-local.timer; config {machine_config})")
            return
        success, message = create_local_scheduled_task()
        if not success:
            raise click.ClickException(message)
        console.print(f"[green]✓[/green] {message} (BackerLocalSchedule as SYSTEM; config {machine_config})")
        return

    from backer.client.agent import BackerAgent

    # Check if registered
    try:
        ag = BackerAgent.from_config()
    except FileNotFoundError:
        console.print("[red]Error:[/red] Agent not registered. Run 'backer agent register' first.")
        raise SystemExit(1)

    if method == "systemd":
        if is_windows():
            console.print("[red]Error:[/red] Systemd not available on Windows")
            raise SystemExit(1)
        success, message = create_systemd_service()
    else:
        if not is_windows():
            console.print("[yellow]Warning:[/yellow] Windows service methods not available on Linux")
            console.print("Use --method systemd instead")
            raise SystemExit(1)

        # Check for admin rights when using service method
        if method == "service" and not is_admin():
            console.print("[yellow]Note:[/yellow] The 'service' method requires Administrator privileges.")
            console.print("Please run this command as Administrator for lockscreen support.")
            console.print()
            console.print("Alternatively, use --method task for user-level installation:")
            console.print("  backer agent install --method task")
            raise SystemExit(1)

        success, message = install_service(method=method, server_url=ag.server_url)

    if success:
        console.print(f"[green]✓[/green] {message}")
    else:
        console.print(f"[red]Error:[/red] {message}")
        raise SystemExit(1)


@agent.command("uninstall")
@click.option("--mode", type=click.Choice(["server", "local"]), default="server", show_default=True)
@click.option("--headless", is_flag=True, help="Remove the machine local scheduler")
@click.option("--keep-config", is_flag=True, help="Keep configuration files")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
def agent_uninstall(mode: str = "server", headless: bool = False, keep_config: bool = False, yes: bool = False) -> None:
    """Remove agent from system startup and optionally uninstall."""
    import shutil
    import subprocess

    from backer.client.windows_service import (
        is_windows,
        remove_local_scheduled_task,
        remove_local_systemd_timer,
        uninstall_service,
    )
    from backer.core.paths import get_config_dir, get_data_dir

    if mode == "local":
        if is_windows():
            if not remove_local_scheduled_task():
                raise click.ClickException("BackerLocalBackup was not removed")
        else:
            remove_local_systemd_timer(headless=headless)
        console.print("[green]✓ Local scheduler removed[/green]")
        return

    if is_windows():
        success, message = uninstall_service()
        if success:
            console.print(f"[green]✓ {message}[/green]")
        else:
            console.print(f"[yellow]{message}[/yellow]")
        return

    # Linux uninstall - check all possible installation locations
    config_dir = get_config_dir()  # /etc/backer
    data_dir = get_data_dir()  # ~/.local/share/backer
    user_service = Path.home() / ".config" / "systemd" / "user" / "backer-agent.service"
    system_service = Path("/etc/systemd/system/backer-agent.service")
    user_bin = Path.home() / ".local" / "bin" / "backer"

    # Also check for install-agent.sh locations
    install_dir = Path("/opt/backer")
    system_bin = Path("/usr/local/bin/backer")

    console.print("[bold]Backer Agent Uninstall[/bold]")
    console.print()

    # Check what exists
    has_user_service = user_service.exists()
    has_system_service = system_service.exists()
    has_config = config_dir.exists()
    has_data = data_dir.exists()
    has_install_dir = install_dir.exists()
    has_system_bin = system_bin.exists() or system_bin.is_symlink()

    if not any([has_user_service, has_system_service, has_config, has_data, has_install_dir, has_system_bin]):
        console.print("[yellow]No agent installation found.[/yellow]")
        return

    if has_system_service:
        console.print(f"  System Service: {system_service}")
    if has_user_service:
        console.print(f"  User Service: {user_service}")
    if has_install_dir:
        console.print(f"  Install Dir: {install_dir}")
    if has_system_bin:
        console.print(f"  Binary: {system_bin}")
    if has_config:
        console.print(f"  Config: {config_dir}")
    if has_data:
        console.print(f"  Data: {data_dir}")
    console.print()

    if not yes:
        if not click.confirm("Uninstall the agent?", default=False):
            console.print("Cancelled.")
            return

    # Stop and disable system service (installed via install-agent.sh)
    if has_system_service:
        console.print("Stopping system service...")
        subprocess.run(["systemctl", "stop", "backer-agent"], capture_output=True)
        subprocess.run(["systemctl", "disable", "backer-agent"], capture_output=True)
        try:
            system_service.unlink(missing_ok=True)
        except PermissionError:
            # Try with sudo via shell
            subprocess.run(["sudo", "rm", "-f", str(system_service)], capture_output=True)
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        console.print("[green]✓ System service removed[/green]")

    # Stop and disable user systemd service (legacy/manual install)
    if has_user_service:
        console.print("Stopping user service...")
        subprocess.run(["systemctl", "--user", "stop", "backer-agent"], capture_output=True)
        subprocess.run(["systemctl", "--user", "disable", "backer-agent"], capture_output=True)
        user_service.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        console.print("[green]✓ User service removed[/green]")

    # Remove user symlink (~/.local/bin/backer)
    if user_bin.exists() or user_bin.is_symlink():
        user_bin.unlink(missing_ok=True)
        console.print("[green]✓ Removed ~/.local/bin/backer[/green]")

    # Remove system symlink (/usr/local/bin/backer)
    if has_system_bin:
        try:
            system_bin.unlink(missing_ok=True)
            console.print(f"[green]✓ Removed {system_bin}[/green]")
        except PermissionError:
            result = subprocess.run(["sudo", "rm", "-f", str(system_bin)], capture_output=True)
            if result.returncode == 0:
                console.print(f"[green]✓ Removed {system_bin}[/green]")
            else:
                console.print(f"[yellow]Could not remove {system_bin} (permission denied)[/yellow]")

    # Remove installation directory (/opt/backer)
    if has_install_dir:
        try:
            shutil.rmtree(install_dir, ignore_errors=False)
            console.print(f"[green]✓ Removed {install_dir}[/green]")
        except PermissionError:
            result = subprocess.run(["sudo", "rm", "-rf", str(install_dir)], capture_output=True)
            if result.returncode == 0:
                console.print(f"[green]✓ Removed {install_dir}[/green]")
            else:
                console.print(f"[yellow]Could not remove {install_dir} (permission denied)[/yellow]")

    # Remove data directory
    if has_data and _is_server_data_dir(data_dir):
        console.print(f"[yellow]Keeping {data_dir}: it contains a Backer server database[/yellow]")
    elif has_data:
        shutil.rmtree(data_dir, ignore_errors=True)
        console.print(f"[green]✓ Removed {data_dir}[/green]")

    # Always remove config on uninstall (no prompting - causes issues with curl|bash)
    if has_config and _is_server_data_dir(config_dir):
        console.print(f"[yellow]Keeping {config_dir}: it contains a Backer server database[/yellow]")
    elif has_config:
        try:
            shutil.rmtree(config_dir, ignore_errors=True)
        except PermissionError:
            # /etc/backer needs sudo
            subprocess.run(["sudo", "rm", "-rf", str(config_dir)], capture_output=True)
        console.print(f"[green]✓ Removed {config_dir}[/green]")

    console.print()
    console.print("[green]Agent uninstalled successfully.[/green]")


@agent.command("progress")
@click.option("--server", "-s", help="Server URL (uses saved config if not specified)")
@click.option("--refresh", "-r", default=1, help="Refresh interval in seconds (default: 1)")
@click.option("--username", envvar="BACKER_ADMIN_USERNAME", help="Backer admin username")
@click.option("--password", envvar="BACKER_API_PASSWORD", help="Backer admin password")
def agent_progress(server: str | None, refresh: int, username: str | None, password: str | None) -> None:
    """Show live progress of running backup jobs.

    Displays a live-updating progress bar for any backups currently running
    on this or other agents. Updates every second by default.

    Examples:
        backer agent progress              # Show progress using saved config
        backer agent progress -s http://backup:8420  # Specify server
        backer agent progress -r 2         # Refresh every 2 seconds
    """
    import sys
    import time
    from datetime import datetime

    import httpx
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
    from rich.table import Table

    from backer.client.agent import BackerAgent

    # Get server URL from config or parameter
    if server:
        server_url = server
    else:
        try:
            ag = BackerAgent.from_config()
            server_url = ag.server_url
        except FileNotFoundError:
            console.print("[red]Error:[/red] No agent config found.")
            console.print("Run 'backer agent register' first or specify --server")
            raise SystemExit(1)

    console.print(f"[dim]Connecting to {server_url}...[/dim]")

    def fetch_running_jobs() -> list[dict]:
        """Fetch currently running backup jobs from server."""
        try:
            response = httpx.get(
                f"{server_url}/api/v1/running",
                timeout=10,
                auth=(username, password) if username and password else None,
            )
            response.raise_for_status()
            data = response.json()

            # Handle different response formats
            if data is None:
                return []
            elif isinstance(data, list):
                return data
            elif isinstance(data, dict):
                jobs = data.get("jobs")
                if jobs is None:
                    return []
                return jobs if isinstance(jobs, list) else []
            else:
                return []
        except httpx.ConnectError:
            return []
        except Exception:
            # Don't hide errors in development
            import traceback

            console.print(f"[dim]Debug: {traceback.format_exc()}[/dim]")
            return []

    def format_bytes(bytes_val: int) -> str:
        """Format bytes to human readable."""
        if not bytes_val or bytes_val == 0:
            return "0 B"
        units = ["B", "KB", "MB", "GB", "TB"]
        i = 0
        while bytes_val >= 1024 and i < len(units) - 1:
            bytes_val /= 1024
            i += 1
        return f"{bytes_val:.1f} {units[i]}"

    def format_duration(started_at: str | None) -> str:
        """Calculate duration since start time."""
        if not started_at:
            return "Unknown"
        try:
            start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            now = datetime.now(start.tzinfo) if start.tzinfo else datetime.now()
            diff = int((now - start).total_seconds())
            if diff < 60:
                return f"{diff}s"
            elif diff < 3600:
                return f"{diff // 60}m {diff % 60}s"
            else:
                return f"{diff // 3600}h {(diff % 3600) // 60}m"
        except Exception:
            return "Unknown"

    def create_display(jobs: list[dict]) -> Panel:
        """Create the display panel showing all running jobs."""
        if not jobs:
            return Panel(
                "[dim]No backup jobs currently running[/dim]\n\n"
                "[yellow]Tip:[/yellow] Start a backup from the web UI or run:\n"
                "  [cyan]backer job run <job-name>[/cyan]",
                title="[bold]Backup Progress[/bold]",
                border_style="blue",
            )

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Content", style="white")

        for job in jobs:
            job_name = job.get("job_name", "Unknown Job")
            progress_pct = job.get("progress_percent", 0) or job.get("progress", 0) or 0
            current_file = job.get("current_file") or job.get("message") or "Processing..."
            bytes_done = job.get("bytes_processed", 0) or job.get("bytes_done", 0) or 0
            bytes_total = job.get("bytes_total", 0) or 0
            files_done = job.get("files_processed", 0) or job.get("files_done", 0) or 0
            started_at = job.get("started_at")

            # Truncate long file paths
            if current_file and len(current_file) > 60:
                current_file = "..." + current_file[-57:]

            # Build progress bar
            progress_bar = Progress(
                SpinnerColumn(),
                TextColumn("[bold]{task.description}"),
                BarColumn(bar_width=30),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            )
            progress_bar.add_task("", completed=progress_pct, total=100)

            # Build info lines
            info_lines = []
            info_lines.append(f"[bold cyan]{job_name}[/bold cyan]")
            info_lines.append(progress_bar)
            info_lines.append(f"[dim]{current_file}[/dim]")

            stats_parts = []
            if bytes_total > 0:
                stats_parts.append(f"{format_bytes(bytes_done)} / {format_bytes(bytes_total)}")
            elif bytes_done > 0:
                stats_parts.append(f"{format_bytes(bytes_done)}")

            if files_done > 0:
                stats_parts.append(f"{files_done:,} files")

            duration = format_duration(started_at)
            if duration != "Unknown":
                stats_parts.append(f"elapsed: {duration}")

            if stats_parts:
                info_lines.append(f"[dim]{' • '.join(stats_parts)}[/dim]")

            # Add to table
            for line in info_lines:
                table.add_row(line)

            # Add spacing between jobs
            table.add_row("")

        return Panel(
            table, title=f"[bold]Backup Progress[/bold] ([green]{len(jobs)}[/green] running)", border_style="green"
        )

    # Live display loop
    try:
        with Live(create_display([]), refresh_per_second=1, console=console) as live:
            while True:
                jobs = fetch_running_jobs()
                live.update(create_display(jobs))
                time.sleep(refresh)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
        sys.exit(0)
    except Exception as e:
        console.print(f"\n[red]Error:[/red] {e}")
        raise SystemExit(1)


@agent.command("update")
@click.option("--dev", is_flag=True, help="Update to the latest dev branch instead of release")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def agent_update(dev: bool, yes: bool) -> None:
    """Update the Backer agent to the latest version.

    Downloads and installs the latest release from GitHub.

    Examples:
        sudo backer agent update           # Update to latest stable release
        sudo backer agent update --dev     # Update to latest dev branch
    """
    import os
    import subprocess
    import sys

    from backer.client.windows_service import is_windows

    if is_windows():
        console.print("[yellow]On Windows, please use the GUI to update:[/yellow]")
        console.print("  Menu > Update")
        console.print()
        console.print("Or download the latest installer from:")
        console.print("  https://git.stockhome.com.au/stocky789/backer/releases/tag/release-main")
        raise SystemExit(0)

    # Check if running as root (needed for system-wide install)
    need_sudo = os.geteuid() != 0

    # Determine source
    if dev:
        source_desc = "dev branch (latest)"
        pip_spec = "git+https://git.stockhome.com.au/stocky789/backer.git@dev"
    else:
        source_desc = "latest stable release"
        pip_spec = "git+https://git.stockhome.com.au/stocky789/backer.git@main"

    console.print("[bold]Backer Agent Update[/bold]\n")
    console.print(f"Current version: {__version__}")
    console.print(f"Updating to: {source_desc}")
    console.print()

    if need_sudo:
        console.print("[yellow]Note:[/yellow] This may require sudo for system-wide install.")
        console.print()

    if not yes:
        if not click.confirm("Continue with update?"):
            console.print("Aborted.")
            raise SystemExit(0)

    # Check if installed via install-agent.sh (in /opt/backer)
    venv_pip = Path("/opt/backer/venv/bin/pip")
    system_service = Path("/etc/systemd/system/backer-agent.service")

    if venv_pip.exists():
        # Installed via install-agent.sh
        console.print("\n[bold]1. Stopping service...[/bold]")
        if system_service.exists():
            subprocess.run(["systemctl", "stop", "backer-agent"], capture_output=True)
            console.print("   [green]✓[/green] Service stopped")

        console.print("\n[bold]2. Updating package...[/bold]")
        try:
            cmd = [str(venv_pip), "install", "--upgrade", pip_spec]
            if need_sudo:
                cmd = ["sudo"] + cmd
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                console.print(f"   [red]✗[/red] Failed: {result.stderr}")
                raise SystemExit(1)
            console.print("   [green]✓[/green] Package updated")
        except Exception as e:
            console.print(f"   [red]✗[/red] Failed: {e}")
            raise SystemExit(1)

        # Get new version
        try:
            result = subprocess.run(
                [str(venv_pip.parent / "python"), "-c", "from backer import __version__; print(__version__)"],
                capture_output=True,
                text=True,
            )
            new_version = result.stdout.strip()
        except Exception:
            new_version = "unknown"

        console.print("\n[bold]3. Restarting service...[/bold]")
        if system_service.exists():
            subprocess.run(["systemctl", "start", "backer-agent"], capture_output=True)
            console.print("   [green]✓[/green] Service started")

        console.print("\n[bold green]✓ Update complete[/bold green]")
        console.print(f"  Previous version: {__version__}")
        console.print(f"  New version: {new_version}")

    else:
        # Installed via pip directly
        console.print("\n[bold]Updating via pip...[/bold]")
        try:
            cmd = [sys.executable, "-m", "pip", "install", "--upgrade", pip_spec]
            if need_sudo:
                cmd = ["sudo"] + cmd
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                console.print(f"[red]✗[/red] Failed: {result.stderr}")
                raise SystemExit(1)
            console.print("[green]✓[/green] Package updated")
            console.print()
            console.print("[bold green]✓ Update complete[/bold green]")
            console.print("Restart the agent to use the new version.")
        except Exception as e:
            console.print(f"[red]✗[/red] Failed: {e}")
            raise SystemExit(1)


@agent.command("logs")
@click.option("-f", "--follow", is_flag=True, help="Follow log output (tail -f style)")
@click.option("-n", "--lines", default=50, help="Number of lines to show (default: 50)")
@click.option("--since", help="Show logs since time (e.g., '1h', '30m', '2024-01-01')")
def agent_logs(follow: bool, lines: int, since: str | None) -> None:
    """View agent logs.

    On Linux, shows systemd journal logs for the backer-agent service.
    On Windows, shows where to find logs.

    Examples:
        backer agent logs              # Show last 50 lines
        backer agent logs -f           # Follow logs in real-time
        backer agent logs -n 100       # Show last 100 lines
        backer agent logs --since 1h   # Show logs from last hour
    """
    import subprocess

    from backer.client.windows_service import is_windows

    if is_windows():
        # Windows doesn't have journald - point to Event Viewer
        console.print("[bold]Windows Agent Logs[/bold]")
        console.print()
        console.print("Agent logs can be found in:")
        console.print("  1. [cyan]Event Viewer[/cyan] > Windows Logs > Application")
        console.print("     Filter by source 'BackerAgent'")
        console.print()
        console.print("  2. [cyan]Task Scheduler[/cyan] > Task Scheduler Library > Backer")
        console.print("     Right-click > View History")
        console.print()
        console.print("For real-time logs, run the agent in foreground mode:")
        console.print("  [dim]backer agent start[/dim]")
        return

    # Linux - use journalctl
    user_service = Path.home() / ".config" / "systemd" / "user" / "backer-agent.service"
    system_service = Path("/etc/systemd/system/backer-agent.service")

    # Determine which service type is installed
    if user_service.exists():
        cmd = ["journalctl", "--user", "-u", "backer-agent"]
    elif system_service.exists():
        cmd = ["journalctl", "-u", "backer-agent"]
    else:
        console.print("[yellow]No systemd service found.[/yellow]")
        console.print()
        console.print("If running the agent manually, logs appear in the terminal.")
        console.print("To install as a service: [cyan]backer agent install --method systemd[/cyan]")
        return

    # Add options
    if follow:
        cmd.append("-f")
    else:
        cmd.extend(["-n", str(lines)])

    if since:
        cmd.extend(["--since", since])

    # Add output formatting
    cmd.append("--no-pager")

    try:
        # Run journalctl - use exec for follow mode to allow Ctrl+C
        if follow:
            console.print("[dim]Following logs (Ctrl+C to stop)...[/dim]")
            console.print()
            subprocess.run(cmd)
        else:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.stdout:
                console.print(result.stdout)
            if result.stderr and "No entries" not in result.stderr:
                console.print(f"[yellow]{result.stderr}[/yellow]")
            if not result.stdout and not result.stderr:
                console.print("[dim]No log entries found.[/dim]")
    except FileNotFoundError:
        console.print("[red]Error:[/red] journalctl not found. Is systemd installed?")
        raise SystemExit(1)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")


# ============ Job commands (for use with server) ============


@main.group()
def job() -> None:
    """Manage local serverless jobs or server-managed jobs selected with --server."""
    pass


@job.command("list")
@click.option("--repo")
@click.option("--server", "-s", help="Server URL")
@click.option("--json", "as_json", is_flag=True)
@click.option("--username", envvar="BACKER_ADMIN_USERNAME", help="Backer admin username")
@click.option("--password", envvar="BACKER_API_PASSWORD", help="Backer admin password")
@click.pass_context
def job_list(
    ctx: click.Context, repo: str | None, server: str | None, as_json: bool, username: str | None, password: str | None
) -> None:
    """List all backup jobs."""
    if repo and server:
        raise click.UsageError("Choose --repo or --server, not both")
    if not server:
        _, config = _local_config(ctx)
        items = [
            {"name": name, **item.model_dump(exclude_none=True)}
            for name, item in config.jobs.items()
            if not repo or item.repository == repo
        ]
        click.echo(json.dumps(items) if as_json else "\n".join(item["name"] for item in items))
        return
    import httpx

    server_url = server or "http://localhost:8420"

    try:
        response = httpx.get(f"{server_url}/api/v1/jobs", auth=(username, password) if username and password else None)
        response.raise_for_status()
        jobs = response.json()

        if not jobs:
            console.print("No jobs configured")
            return

        table = Table(title="Backup Jobs")
        table.add_column("Name", style="cyan")
        table.add_column("Source")
        table.add_column("Destination")
        table.add_column("Last Run")
        table.add_column("Status")

        for j in jobs:
            last_status = j.get("last_status")
            if last_status == "success":
                status_style = "green"
            elif last_status == "failed":
                status_style = "red"
            else:
                status_style = "dim"
            table.add_row(
                j["name"],
                j.get("source_path", "")[:30],
                j.get("destination_path", "")[:30],
                j.get("last_run", "-")[:19] if j.get("last_run") else "-",
                f"[{status_style}]{j.get('last_status', '-')}[/{status_style}]",
            )

        console.print(table)

    except httpx.ConnectError:
        console.print(f"[red]Error:[/red] Cannot connect to server at {server_url}")
        raise SystemExit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)


@job.command("create")
@click.argument("name", required=False)
@click.option("--name", "legacy_name", help="Job name")
@click.option("--source", "-s", required=True, multiple=True, help="Source path")
@click.option("--exclude", multiple=True)
@click.option("--dest", "-d", help="Destination path")
@click.option("--schedule", help="Cron schedule (e.g., '0 2 * * *')")
@click.option("--daily")
@click.option("--no-schedule", is_flag=True)
@click.option("--repo", "--repository", "repository_id", help="Local serverless repository id")
@click.option("--keep-last", type=click.IntRange(min=1))
@click.option("--keep-daily", type=click.IntRange(min=1))
@click.option("--keep-weekly", type=click.IntRange(min=1))
@click.option("--keep-monthly", type=click.IntRange(min=1))
@click.option("--keep-yearly", type=click.IntRange(min=1))
@click.option("--server", help="Server URL")
@click.option("--username", envvar="BACKER_ADMIN_USERNAME", help="Backer admin username")
@click.option("--password", envvar="BACKER_API_PASSWORD", help="Backer admin password")
@click.pass_context
def job_create(
    ctx: click.Context,
    name: str | None,
    legacy_name: str | None,
    source: tuple[str, ...],
    exclude: tuple[str, ...],
    dest: str | None,
    schedule: str | None,
    daily: str | None,
    no_schedule: bool,
    repository_id: str | None,
    keep_last: int | None,
    keep_daily: int | None,
    keep_weekly: int | None,
    keep_monthly: int | None,
    keep_yearly: int | None,
    server: str | None,
    username: str | None,
    password: str | None,
) -> None:
    """Create a new backup job."""
    name = name or legacy_name
    if not name:
        raise click.UsageError("Missing argument 'NAME'")
    if len(source) != 1:
        raise click.UsageError("Exactly one --source is required in this build")
    if sum(bool(value) for value in (schedule, daily, no_schedule)) != 1:
        raise click.UsageError("Choose exactly one of --schedule, --daily or --no-schedule")
    if daily:
        import re

        if not re.fullmatch(r"\d{2}:\d{2}", daily):
            raise click.UsageError("--daily must be HH:MM")
        schedule = f"{daily.split(':')[1]} {daily.split(':')[0]} * * *"
    source_path = source[0]
    if repository_id and server:
        raise click.UsageError("Choose --repo or --server, not both")
    if not server:
        from backer.core.config import JobConfig, RetentionConfig, ScheduleConfig, SourceConfig, load_config
        from backer.core.paths import get_config_dir

        path = ctx.obj.get("config_path") or get_config_dir() / "config.yaml"
        config = load_config(path)
        repository_id = _resolve_job_repository(config, repository_id)
        owner = next(
            (
                other
                for other, item in config.jobs.items()
                if item.repository == repository_id and item.source.path == source_path
            ),
            None,
        )
        if owner:
            raise click.ClickException(f"Source '{source}' is already owned by job '{owner}'")
        retention = RetentionConfig(
            keep_last=keep_last,
            keep_daily=keep_daily,
            keep_weekly=keep_weekly,
            keep_monthly=keep_monthly,
            keep_yearly=keep_yearly,
        )
        config.jobs[name] = JobConfig(
            repository=repository_id,
            source=SourceConfig(path=source_path, excludes=list(exclude)),
            schedule=ScheduleConfig(cron=schedule) if schedule and not no_schedule else None,
            retention=retention if any(retention.model_dump().values()) else None,
        )
        config.save(path)
        console.print(f"Job '{name}' created")
        return
    if not dest:
        raise click.UsageError("--dest is required for server-managed jobs")
    import httpx

    server_url = server or "http://localhost:8420"

    job_data = {
        "name": name,
        "source_path": source_path,
        "destination_path": dest,
        "schedule_cron": schedule,
    }

    try:
        response = httpx.post(
            f"{server_url}/api/v1/jobs",
            json=job_data,
            auth=(username, password) if username and password else None,
        )
        response.raise_for_status()

        console.print(f"[green]✓ Job '{name}' created[/green]")

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 409:
            console.print(f"[red]Error:[/red] Job '{name}' already exists")
        else:
            console.print(f"[red]Error:[/red] {e.response.text}")
        raise SystemExit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)


@job.command("history")
@click.argument("name")
@click.option("--limit", default=20, show_default=True, type=click.IntRange(min=1))
@click.option("--repo")
@click.option("--server")
@click.option("--json", "as_json", is_flag=True)
def job_history(name: str, limit: int, repo: str | None, server: str | None, as_json: bool) -> None:
    """Show local serverless attempt history."""
    from backer.core.config import load_config
    from backer.core.paths import get_config_dir, get_data_dir
    from backer.serverless.store import read_runs

    if repo and server:
        raise click.UsageError("Choose --repo or --server, not both")
    if server:
        raise click.ClickException("Server job history is not implemented by the local CLI")
    config = load_config(get_config_dir() / "config.yaml")
    _resolve_job_repository(config, repo, name)
    attempts = read_runs(get_data_dir(), name, limit)
    if as_json:
        click.echo(json.dumps([item.to_dict() for item in attempts], default=str))
    else:
        for attempt in attempts:
            console.print(
                f"{attempt.started_at.isoformat()} {attempt.status.value} {attempt.error_message or ''}".rstrip()
            )


@job.command("show")
@click.argument("name")
@click.option("--repo")
@click.option("--server")
@click.option("--last", "last_run", is_flag=True)
@click.option("--verbose", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def job_show(
    ctx: click.Context, name: str, repo: str | None, server: str | None, last_run: bool, verbose: bool, as_json: bool
) -> None:
    if repo and server:
        raise click.UsageError("Choose --repo or --server, not both")
    if server:
        raise click.ClickException("Server job details are not implemented by the local CLI")
    _, config = _local_config(ctx)
    configured = config.jobs.get(name)
    if not configured:
        raise click.ClickException(f"Job '{name}' is not configured")
    if repo and configured.repository != _resolve_job_repository(config, repo, name):
        raise click.ClickException(f"Job '{name}' is not configured for repository '{repo}'")
    payload = configured.model_dump(exclude_none=True)
    click.echo(json.dumps(payload) if as_json else f"{name}: {payload}")


@job.command("rm")
@click.argument("name")
@click.option("--repo")
@click.option("--server")
@click.option("--yes", is_flag=True)
@click.pass_context
def job_rm(ctx: click.Context, name: str, repo: str | None, server: str | None, yes: bool) -> None:
    if bool(repo) == bool(server):
        raise click.UsageError("Choose exactly one of --repo or --server")
    if not yes:
        raise click.UsageError("--yes is required to remove a job")
    if server:
        raise click.ClickException("Server job removal is not implemented by the local CLI")
    path, config = _local_config(ctx)
    configured = config.jobs.get(name)
    if not configured or configured.repository != repo:
        raise click.ClickException(f"Job '{name}' is not configured for repository '{repo}'")
    del config.jobs[name]
    config.save(path)
    click.echo(f"Job '{name}' removed")


@main.command("prune")
@click.argument("name")
@click.option("--list", "list_only", is_flag=True)
@click.option("--apply", is_flag=True, help="Delete the snapshots shown by the preview")
@click.option("--yes-remove", type=int)
@click.option("--json", "as_json", is_flag=True)
def prune(name: str, list_only: bool, apply: bool, yes_remove: int | None, as_json: bool) -> None:
    """Preview a job's retention; pass --apply to delete."""
    from backer.core.config import load_config
    from backer.core.paths import get_config_dir
    from backer.serverless.retention import prune_job

    if apply and yes_remove is None:
        raise click.UsageError("--yes-remove N is required with --apply")
    config = load_config(get_config_dir() / "config.yaml")
    try:
        count, preview = prune_job(config, name, apply=False)
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    if apply and yes_remove != count:
        raise click.UsageError(f"--yes-remove must equal the previewed count ({count})")
    if apply:
        try:
            refreshed_count, refreshed_preview = prune_job(config, name, apply=False)
        except ValueError as error:
            raise click.ClickException(str(error)) from error
        if refreshed_count != count or refreshed_preview != preview:
            raise click.ClickException("Retention preview changed; nothing was deleted")
        try:
            count, _ = prune_job(config, name, apply=True)
        except ValueError as error:
            raise click.ClickException(str(error)) from error
    message = f"{count} snapshot(s) {'deleted' if apply else 'would be deleted'}"
    click.echo(json.dumps({"count": count, "applied": apply}) if as_json else message)


@main.command("snapshots")
@click.argument("job", required=False)
@click.option("--repo")
@click.option("--host")
@click.option("--source")
@click.option("--limit", type=click.IntRange(min=1))
@click.option("--all", "all_snapshots", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def snapshots(
    ctx: click.Context,
    job: str | None,
    repo: str | None,
    host: str | None,
    source: str | None,
    limit: int | None,
    all_snapshots: bool,
    as_json: bool,
) -> None:
    """List snapshots from one local repository."""
    if not job and not repo:
        raise click.UsageError("JOB or --repo NAME is required")
    from backer.serverless.repositories import probe

    _, config = _local_config(ctx)
    repository_id = _resolve_job_repository(config, repo, job)
    record = config.repositories[repository_id]
    backend, passphrase, storage = _repository_backend(record)
    rows = backend.list_snapshots(BackupDestination(_repository_destination(record)))
    if source:
        rows = [item for item in rows if item.get("paths") == [source]]
    if host:
        rows = [item for item in rows if item.get("hostname") == host]
    if job and not all_snapshots:
        rows = [item for item in rows if item.get("paths") == [config.jobs[job].source.path]]
    if not rows:
        status, _, message = probe(record, passphrase, storage)
        if status != "present":
            raise click.ClickException(message or f"Repository is {status}")
    rows = sorted(rows, key=lambda item: item.get("timestamp") or "", reverse=True)
    if limit:
        rows = rows[:limit]
    if as_json:
        click.echo(json.dumps(rows))
    elif rows:
        for row in rows:
            click.echo(f"{row.get('id')}\t{row.get('timestamp')}\t{row.get('paths', [''])[0]}")
    else:
        click.echo("No snapshots yet")


@main.command("verify")
@click.argument("job")
@click.option("--restore-test", is_flag=True)
@click.option("--verify-files-percent", type=click.FloatRange(min=0, max=100))
@click.option("--repair-index", is_flag=True)
@click.option("--timeout", type=click.IntRange(min=1))
def verify(
    job: str, restore_test: bool, verify_files_percent: float | None, repair_index: bool, timeout: int | None
) -> None:
    """Verify a local repository; index recovery is always explicit."""
    from backer.core.config import load_config
    from backer.core.paths import get_config_dir

    config = load_config(get_config_dir() / "config.yaml")
    configured = config.jobs.get(job)
    if not configured:
        raise click.ClickException(f"Job '{job}' is not configured")
    record = config.repositories.get(configured.repository)
    if not record:
        raise click.ClickException(f"Job '{job}' names an unknown repository")
    backend, passphrase, _ = _repository_backend(record, timeout=timeout)
    destination = BackupDestination(_repository_destination(record))
    if repair_index:
        preview = backend.repair_index(destination, commit=False)
        if not preview.success:
            raise click.ClickException(
                _redact_error(RuntimeError(preview.output or "; ".join(preview.errors)), passphrase)
            )
        click.echo(_redact_error(RuntimeError(preview.output), passphrase))
        if not _interactive():
            raise click.UsageError(
                "Index recovery preview completed; --repair-index requires an interactive confirmation"
            )
        if not click.confirm("Commit this index recovery", default=False):
            click.echo("Index recovery was not committed")
            return
        result = backend.repair_index(destination, commit=True)
    else:
        if verify_files_percent is not None:
            click.echo(
                f"Checking {verify_files_percent:g}% downloads and rehashes real file contents. "
                "It may take longer on this connection."
            )
        result = backend.check(destination, verify_files_percent=verify_files_percent)
    if not result.success:
        detail = _redact_error(RuntimeError(result.output or "; ".join(result.errors)), passphrase)
        timeout_detail = " ".join([result.output, *result.errors]).lower()
        if "timeout" in timeout_detail or "timed out" in timeout_detail:
            raise click.ClickException(
                f"The check ran out of time after {timeout or 3600} seconds. "
                "This does not mean the repository is damaged. "
                f"Run it again with --timeout {(timeout or 3600) * 2}.\n{detail}"
            )
        from backer.core.messages import explain_failure

        raise click.ClickException(
            "The backup engine reported errors in this repository. Existing snapshots may not restore completely.\n"
            f"{explain_failure(detail)}\n{detail}\n"
            f"Do not run backer prune. Check the file server, then run: backer verify {job} --repair-index"
        )
    click.echo("Repository check passed: snapshot manifests are indexed and reachable")
    if verify_files_percent is None:
        click.echo(
            "File contents were not downloaded or restored. "
            "Use --verify-files-percent N to download and hash a sample."
        )
    else:
        click.echo(f"Checked {verify_files_percent:g}% of file contents. This downloads and hashes file data.")
    if restore_test:
        snapshots = backend.list_snapshots(destination)
        if not snapshots:
            raise click.ClickException("Restore test needs a reachable repository with a snapshot")
        newest = max(snapshots, key=lambda item: item.get("timestamp") or "")
        count, skipped = _run_restore_test(backend, destination, newest)
        click.echo(
            f"Backer restored {count} files from the newest snapshot and they match files on this computer. "
            "This tests the repository, not every file in it."
        )
        if skipped:
            click.echo(f"Skipped {skipped} sampled file(s) no longer present on this computer.")


@main.command("status")
@click.argument("job", required=False)
@click.option("--why", is_flag=True)
@click.option("--exit-code", is_flag=True, help="Exit non-zero for failed or stale local jobs")
@click.option("--json", "as_json", is_flag=True)
def status(job: str | None, why: bool, exit_code: bool, as_json: bool) -> None:
    """Show local serverless backup status."""
    from datetime import UTC, datetime

    from backer.core.config import load_config
    from backer.core.paths import get_config_dir, get_data_dir
    from backer.serverless.store import read_runs

    config = load_config(get_config_dir() / "config.yaml")
    unhealthy = False
    now = datetime.now(UTC)
    results = []
    for name, configured in config.jobs.items():
        if job and name != job:
            continue
        if not configured.enabled:
            continue
        attempts = read_runs(get_data_dir(), name, 1)
        latest = attempts[0] if attempts else None
        failed = latest is None or latest.status.value != "success"
        if latest and configured.schedule and configured.schedule.cron:
            stale_before = _stale_cutoff(configured.schedule.cron, now)
            failed = failed or latest.started_at.astimezone(UTC) < stale_before.astimezone(UTC)
        message = latest.error_message if latest and latest.error_message else None
        results.append({"job": name, "status": "failed" if failed else "success", "message": message})
        if not as_json:
            console.print(f"{name}: {'failed' if failed else 'success'}")
            if why and message:
                from backer.core.messages import failure_details

                record = config.repositories.get(configured.repository)
                location = "this repository"
                account = None
                if record:
                    if record.type == "smb":
                        location = "\\\\" + "\\".join(
                            part for part in (record.server, record.share, record.path) if part
                        )
                        account = record.username
                    elif record.type == "local":
                        location = record.path or location
                console.print(failure_details(message))
                console.print(f"Repository: {location}" + (f" as {account}" if account else ""))
                console.print(f"Try now: backer job run {name}")
                console.print(f"Details and logs: {get_data_dir() / 'logs'}")
        unhealthy = unhealthy or failed
    if as_json:
        click.echo(json.dumps(results))
    if exit_code and unhealthy:
        raise click.ClickException("One or more backups need attention")


@job.command("run")
@click.argument("name", required=False)
@click.option("--all", "all_jobs", is_flag=True)
@click.option("--due", is_flag=True, help="Run locally due serverless jobs")
@click.option("--repo")
@click.option("--dry-run", "-n", is_flag=True, help="Simulate without making changes")
@click.option("--progress/--no-progress", default=None)
@click.option("--server", help="Server URL")
@click.option("--username", envvar="BACKER_ADMIN_USERNAME", help="Backer admin username")
@click.option("--password", envvar="BACKER_API_PASSWORD", help="Backer admin password")
@click.pass_context
def job_run(
    ctx: click.Context,
    name: str | None,
    all_jobs: bool,
    due: bool,
    repo: str | None,
    dry_run: bool,
    progress: bool | None,
    server: str | None,
    username: str | None,
    password: str | None,
) -> None:
    """Run a backup job."""
    from backer.core.config import load_config
    from backer.core.paths import get_config_dir

    if repo and server:
        raise click.UsageError("Choose --repo or --server, not both")
    local_config = load_config(ctx.obj.get("config_path") or get_config_dir() / "config.yaml")
    if all_jobs:
        names = (
            [item for item, configured in local_config.jobs.items() if configured.repository == repo]
            if repo
            else list(local_config.jobs)
        )
        if not names:
            raise click.ClickException("No local jobs match the selection")
        from backer.serverless.runs import run_local_job

        for item in names:
            with _local_progress(progress) as render:
                report = _local_job_call(lambda: run_local_job(local_config, item, on_progress=render))
            if not report or not report["success"]:
                raise click.ClickException("; ".join((report or {}).get("errors") or ["Backup failed"]))
        return
    if due or (name and name in local_config.jobs):
        if name:
            _resolve_job_repository(local_config, repo, name)
        from backer.serverless.runs import run_due_jobs, run_local_job

        try:
            system_run = os.environ.get("BACKER_RUN_AS_SYSTEM") == "1"
            if due:
                reports = _local_job_call(lambda: run_due_jobs(local_config, run_as_system=system_run))
            else:
                with _local_progress(progress) as render:
                    reports = _local_job_call(
                        lambda: run_local_job(local_config, name, run_as_system=system_run, on_progress=render)
                    )
        except ValueError as error:
            raise click.ClickException(str(error)) from error
        if reports is None:
            if due:
                console.print("Another local backup is running")
                return
            raise click.ClickException("Another local backup is running")
        if due and not reports:
            console.print("No jobs due")
        for report in reports if due else [reports]:
            if not report["success"]:
                raise click.ClickException("; ".join(report.get("errors") or ["Backup failed"]))
        return
    if not name:
        raise click.UsageError("NAME, --all, or --due is required")
    if not server:
        raise click.ClickException(f"Job '{name}' is not configured locally; pass --server for a server job")
    import httpx

    server_url = server or "http://localhost:8420"

    try:
        response = httpx.post(
            f"{server_url}/api/v1/jobs/{name}/run",
            json={"dry_run": dry_run},
            auth=(username, password) if username and password else None,
        )
        response.raise_for_status()
        result = response.json()

        console.print("[green]✓ Job started[/green]")
        console.print(f"  Run ID: {result['run_id']}")
        console.print(f"  Status: {result['status']}")

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Job '{name}' not found")
        else:
            console.print(f"[red]Error:[/red] {e.response.text}")
        raise SystemExit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
