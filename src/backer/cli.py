"""Backer CLI - unified backup management."""

import os
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from backer import __version__
from backer.backends.base import BackupDestination, BackupSource

console = Console()


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
        console.print("\n[bold]Setup complete![/bold]")
        console.print("Run 'backer tools' to see Kopia status.")


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
@click.option(
    "--repository-password",
    envvar="BACKER_REPOSITORY_PASSWORD",
    required=True,
    help="Repository encryption password",
)
@click.option("--exclude", "-e", multiple=True, help="Exclude patterns")
@click.option("--dry-run", "-n", is_flag=True, help="Simulate without making changes")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
def backup(
    source: Path,
    destination: str,
    repository_password: str,
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
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)


@main.command()
@click.argument("source")
@click.argument("destination", type=click.Path(path_type=Path))
@click.option(
    "--repository-password",
    envvar="BACKER_REPOSITORY_PASSWORD",
    required=True,
    help="Repository encryption password",
)
@click.option("--snapshot", "-s", help="Snapshot ID to restore")
@click.option("--dry-run", "-n", is_flag=True, help="Simulate without making changes")
def restore(
    source: str,
    destination: Path,
    repository_password: str,
    snapshot: str | None,
    dry_run: bool,
) -> None:
    """Restore from a backup.

    SOURCE is the backup location.
    DESTINATION is where to restore files.
    """
    from backer.backends.kopia import KopiaBackend

    console.print(f"[bold]Restoring[/bold] {source} → {destination}")

    try:
        be = KopiaBackend({"repository_password": repository_password})
        src = BackupDestination(path=source)

        with console.status("Restoring..."):
            result = be.restore(source=src, destination=destination, snapshot=snapshot, dry_run=dry_run)

        if result.success:
            console.print("[green]✓ Restore completed[/green]")
        else:
            console.print("[red]✗ Restore failed[/red]")
            for error in result.errors:
                console.print(f"  [red]{error}[/red]")
            raise SystemExit(1)

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)


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
            console.print("   [yellow]![/yellow] Service may not be running properly")
            console.print("   Check logs with: journalctl -u backer -n 50")
    except Exception:
        pass

    console.print("\n[bold green]✓ Update complete![/bold green]")
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
        raise click.UsageError("Choose exactly one of --passphrase-stdin or --passphrase-file")
    value = sys.stdin.read() if stream else file.read_text(encoding="utf-8")
    value = value.rstrip("\r\n")
    if not value:
        raise click.UsageError("Repository passphrase is required")
    return value


@repo.command("add")
@click.argument("name")
@click.option("--attach", is_flag=True, help="Attach an existing repository")
@click.option("--init", "initialize", is_flag=True, help="Create a new repository")
@click.option("--type", "repository_type", type=click.Choice(["local", "smb", "s3"]), default="local")
@click.option("--path")
@click.option("--passphrase-stdin", is_flag=True)
@click.option("--passphrase-file", type=click.Path(exists=True, path_type=Path))
@click.option("--headless", is_flag=True, help="Allow the protected local-file secret fallback")
@click.pass_context
def repo_add(
    ctx: click.Context,
    name: str,
    attach: bool,
    initialize: bool,
    repository_type: str,
    path: str | None,
    passphrase_stdin: bool,
    passphrase_file: Path | None,
    headless: bool,
) -> None:
    """Attach or explicitly create one repository."""
    from backer.core.config import BackerConfig, RepositoryConfig, load_config
    from backer.core.paths import get_config_dir
    from backer.serverless.repositories import add_repository

    try:
        passphrase = _read_passphrase(passphrase_stdin, passphrase_file)
        config_path = ctx.obj.get("config_path") or get_config_dir() / "config.yaml"
        config = load_config(config_path) if config_path.exists() else BackerConfig()
        record = RepositoryConfig(name=name, type=repository_type, path=path)
        repo_id, backend = add_repository(
            config, config_path, name, record, passphrase, attach=attach, init=initialize, headless=headless
        )
        console.print(f"Repository '{name}' saved ({backend}, id {repo_id})")
        if backend == "file":
            console.print("Warning: secrets are stored in protected local files")
    except (OSError, ValueError) as error:
        raise click.ClickException(str(error)) from error


@repo.command("adopt")
@click.argument("name")
@click.option("--job", "jobs", multiple=True, help="Sidecar job name to import (repeatable)")
@click.option("--all", "all_jobs", is_flag=True, help="Import every discovered sidecar job")
@click.option("--source", "sources", multiple=True, help="NAME=local source path")
@click.pass_context
def repo_adopt(ctx: click.Context, name: str, jobs: tuple[str, ...], all_jobs: bool, sources: tuple[str, ...]) -> None:
    """Copy existing repository sidecar jobs into this machine's config."""
    import json

    from backer.core import keystore
    from backer.core.config import load_config
    from backer.core.paths import get_config_dir
    from backer.core.repo_metadata import RepositoryMetadata
    from backer.serverless.repositories import _destination, probe
    from backer.serverless.sidecar import adopt_jobs

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
    root = Path(_destination(record))
    discovery = RepositoryMetadata(root, record.type).discover_all()
    available = [item.get("job_name") for item in discovery["jobs"] if item.get("job_name")]
    selected = available if all_jobs else list(jobs)
    if not selected:
        raise click.ClickException("Choose --job NAME or --all")
    mapping = dict(item.split("=", 1) for item in sources if "=" in item)
    try:
        adopted = adopt_jobs(config, repository_id, root, selected, source_paths=mapping)
        config.save(config_path)
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    for job_name in adopted:
        console.print(f"Adopted {job_name}")


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
        from backer.core.config import load_config
        from backer.core.paths import get_config_dir, get_machine_config_dir
        from backer.serverless.repositories import rescope_secrets_for_system

        config = load_config(get_config_dir() / "config.yaml")
        try:
            rescope_secrets_for_system(config)
        except ValueError as error:
            raise click.ClickException(str(error)) from error
        config.save(get_machine_config_dir() / "config.yaml")
        if not is_windows():
            success, message = create_local_systemd_timer(headless=headless)
            if not success:
                raise click.ClickException(message)
            console.print(f"[green]✓[/green] {message}")
            return
        success, message = create_local_scheduled_task()
        if not success:
            raise click.ClickException(message)
        console.print(f"[green]✓[/green] {message}")
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
def agent_uninstall(mode: str, headless: bool, keep_config: bool, yes: bool) -> None:
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

        console.print("\n[bold green]✓ Update complete![/bold green]")
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
            console.print("[bold green]✓ Update complete![/bold green]")
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
    """Job management commands (requires server)."""
    pass


@job.command("list")
@click.option("--server", "-s", help="Server URL")
@click.option("--username", envvar="BACKER_ADMIN_USERNAME", help="Backer admin username")
@click.option("--password", envvar="BACKER_API_PASSWORD", help="Backer admin password")
def job_list(server: str | None, username: str | None, password: str | None) -> None:
    """List all backup jobs."""
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
@click.option("--name", "-n", required=True, help="Job name")
@click.option("--source", "-s", required=True, help="Source path")
@click.option("--dest", "-d", required=True, help="Destination path")
@click.option("--schedule", help="Cron schedule (e.g., '0 2 * * *')")
@click.option("--server", help="Server URL")
@click.option("--username", envvar="BACKER_ADMIN_USERNAME", help="Backer admin username")
@click.option("--password", envvar="BACKER_API_PASSWORD", help="Backer admin password")
def job_create(
    name: str,
    source: str,
    dest: str,
    schedule: str | None,
    server: str | None,
    username: str | None,
    password: str | None,
) -> None:
    """Create a new backup job."""
    import httpx

    server_url = server or "http://localhost:8420"

    job_data = {
        "name": name,
        "source_path": source,
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


@job.command("run")
@click.argument("name", required=False)
@click.option("--due", is_flag=True, help="Run locally due serverless jobs")
@click.option("--dry-run", "-n", is_flag=True, help="Simulate without making changes")
@click.option("--server", help="Server URL")
@click.option("--username", envvar="BACKER_ADMIN_USERNAME", help="Backer admin username")
@click.option("--password", envvar="BACKER_API_PASSWORD", help="Backer admin password")
def job_run(
    name: str | None, due: bool, dry_run: bool, server: str | None, username: str | None, password: str | None
) -> None:
    """Run a backup job."""
    from backer.core.config import load_config
    from backer.core.paths import get_config_dir

    local_config = load_config(get_config_dir() / "config.yaml")
    if due or (name and name in local_config.jobs):
        from backer.serverless.runs import run_due_jobs, run_local_job

        try:
            system_run = os.environ.get("BACKER_RUN_AS_SYSTEM") == "1"
            reports = (
                run_due_jobs(local_config, run_as_system=system_run) if due else [run_local_job(local_config, name)]
            )
        except ValueError as error:
            raise click.ClickException(str(error)) from error
        if due and not reports:
            console.print("No jobs due")
        for report in reports:
            if not report["success"]:
                raise click.ClickException("Backup failed")
        return
    if not name:
        raise click.UsageError("NAME is required unless --due is used")
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
