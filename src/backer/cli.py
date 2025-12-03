"""Backer CLI - unified backup management."""

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from backer import __version__

# Import backends to register them
from backer.backends import BackendRegistry, kopia, rclone, restic, rsync  # noqa: F401
from backer.backends.base import BackupDestination, BackupSource

console = Console()


@click.group()
@click.version_option(version=__version__)
@click.option("--config", "-c", type=click.Path(path_type=Path), help="Config file path")
@click.pass_context
def main(ctx: click.Context, config: Path | None) -> None:
    """Backer - Unified backup orchestration.

    Consolidates rsync, rclone, restic, kopia and more into one tool.

    Run 'backer setup' to download and install backup tools.
    """
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


# ============ Setup and tool management ============


@main.command()
@click.option("--force", "-f", is_flag=True, help="Force reinstall even if already installed")
def setup(force: bool) -> None:
    """Download and install backup tools (rclone, restic).

    This automatically downloads the required backup tools so you don't
    need to install them manually.
    """
    from backer.tools.manager import get_tool_manager

    manager = get_tool_manager()

    console.print("[bold]Backer Setup[/bold]")
    console.print(f"Tools directory: {manager.tools_dir}\n")

    tools = ["rclone", "restic"]

    for tool in tools:
        existing = manager.get_tool_path(tool)

        if existing and not force:
            version = manager.get_version(tool)
            console.print(f"[green]✓[/green] {tool} already installed: {version}")
            console.print(f"  Path: {existing}")
        else:
            console.print(f"[yellow]→[/yellow] Downloading {tool}...")
            try:
                path = manager.download(tool, progress_callback=lambda msg: console.print(f"  {msg}"))
                version = manager.get_version(tool)
                console.print(f"[green]✓[/green] {tool} installed: {version}")
                console.print(f"  Path: {path}")
            except Exception as e:
                console.print(f"[red]✗[/red] Failed to install {tool}: {e}")

    console.print("\n[bold]Setup complete![/bold]")
    console.print("Run 'backer tools' to see installed tools.")


@main.command()
def tools() -> None:
    """Show status of backup tools."""
    from backer.tools.manager import get_tool_manager

    manager = get_tool_manager()
    tools_info = manager.list_tools()

    table = Table(title="Backup Tools")
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
    console.print("Run 'backer setup' to install missing tools.")


@main.command()
def backends() -> None:
    """List available backup backends and their status."""
    table = Table(title="Available Backends")
    table.add_column("Backend", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("Version/Info")

    for backend_type, (available, info) in BackendRegistry.check_all_available().items():
        status = "[green]✓ Available[/green]" if available else "[red]✗ Not found[/red]"
        table.add_row(backend_type.value, status, info)

    console.print(table)


# ============ Standalone backup commands ============


@main.command()
@click.argument("source", type=click.Path(exists=True, path_type=Path))
@click.argument("destination")
@click.option("--backend", "-b", default="rclone", help="Backend to use (rclone, restic, rsync)")
@click.option("--exclude", "-e", multiple=True, help="Exclude patterns")
@click.option("--dry-run", "-n", is_flag=True, help="Simulate without making changes")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
def backup(
    source: Path,
    destination: str,
    backend: str,
    exclude: tuple[str, ...],
    dry_run: bool,
    verbose: bool,
) -> None:
    """Run a one-off backup.

    SOURCE is the local path to back up.
    DESTINATION is where to store the backup (local path or remote URI).

    Examples:

        backer backup /home/user/docs /mnt/backup/docs

        backer backup /data remote:bucket/data -b rclone

        backer backup /home /backup/repo -b restic
    """
    from backer.backends import get_backend

    console.print(f"[bold]Backing up[/bold] {source} → {destination}")
    console.print(f"Backend: {backend}" + (" (dry run)" if dry_run else ""))

    try:
        be = get_backend(backend)

        with console.status(f"Checking {backend} availability..."):
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
@click.option("--backend", "-b", default="rclone", help="Backend to use")
@click.option("--snapshot", "-s", help="Snapshot ID to restore (for restic/borg)")
@click.option("--dry-run", "-n", is_flag=True, help="Simulate without making changes")
def restore(
    source: str,
    destination: Path,
    backend: str,
    snapshot: str | None,
    dry_run: bool,
) -> None:
    """Restore from a backup.

    SOURCE is the backup location.
    DESTINATION is where to restore files.
    """
    from backer.backends import get_backend

    console.print(f"[bold]Restoring[/bold] {source} → {destination}")

    try:
        be = get_backend(backend)
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


@server.command("start")
@click.option("--host", "-h", default="0.0.0.0", help="Host to bind to")
@click.option("--port", "-p", default=8420, help="Port to listen on")
@click.option("--data-dir", type=click.Path(path_type=Path), help="Data directory")
def server_start(host: str, port: int, data_dir: Path | None) -> None:
    """Start the Backer server."""
    console.print(f"[bold]Starting Backer server[/bold] on {host}:{port}")

    try:
        from backer.server.daemon import run_server
        run_server(host=host, port=port, data_dir=data_dir)
    except ImportError as e:
        console.print(f"[red]Error:[/red] Server dependencies not installed: {e}")
        console.print("Install with: pip install backer[server]")
        raise SystemExit(1)


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
    console.print("[dim]  rm -rf ~/backer ~/venv[/dim]")


# ============ Agent commands ============


@main.group()
def agent() -> None:
    """Agent management commands."""
    pass


@agent.command("register")
@click.option("--server", "-s", required=True, help="Server URL (e.g., http://backup-server:8420)")
def agent_register(server: str) -> None:
    """Register this machine as a backup client."""
    from backer.client.agent import BackerAgent

    console.print(f"Registering with server: {server}")

    try:
        ag = BackerAgent(server_url=server)
        client_id, _ = ag.register()
        console.print("[green]✓ Registered successfully[/green]")
        console.print(f"  Client ID: {client_id}")
        console.print(f"  Config saved to: {ag.config_path}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)


@agent.command("start")
@click.option("--server", "-s", help="Server URL (uses saved config if not specified)")
def agent_start(server: str | None) -> None:
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
                ag.register()
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
    from backer.client.windows_service import get_task_status, is_windows

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
            task_info = get_task_status()
            if task_info["installed"]:
                console.print(f"[dim]Service: {task_info.get('status', 'Unknown')}[/dim]")
            else:
                console.print("[dim]Service: Not installed (run 'backer agent install')[/dim]")

    except FileNotFoundError:
        console.print("[yellow]Agent not registered[/yellow]")
        console.print("Run 'backer agent register --server <url>' to register")


@agent.command("install")
@click.option("--method", "-m", default="task", type=click.Choice(["task", "startup", "systemd"]),
              help="Installation method (Windows: task/startup, Linux: systemd)")
def agent_install(method: str) -> None:
    """Install agent to run at system startup.

    On Windows, creates a scheduled task or startup script.
    On Linux, creates a systemd user service.

    Examples:
        backer agent install                  # Default: scheduled task on Windows
        backer agent install --method startup # Use startup folder instead
        backer agent install --method systemd # Linux systemd service
    """
    from backer.client.agent import BackerAgent
    from backer.client.windows_service import create_systemd_service, install_service, is_windows

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
        success, message = install_service(method=method, server_url=ag.server_url)

    if success:
        console.print(f"[green]✓ {message}[/green]")
    else:
        console.print(f"[red]Error:[/red] {message}")
        raise SystemExit(1)


@agent.command("uninstall")
def agent_uninstall() -> None:
    """Remove agent from system startup."""
    from backer.client.windows_service import is_windows, uninstall_service

    if is_windows():
        success, message = uninstall_service()
        if success:
            console.print(f"[green]✓ {message}[/green]")
        else:
            console.print(f"[yellow]{message}[/yellow]")
    else:
        console.print("Run: systemctl --user disable backer-agent")
        console.print("Then delete ~/.config/systemd/user/backer-agent.service")


# ============ Job commands (for use with server) ============


@main.group()
def job() -> None:
    """Job management commands (requires server)."""
    pass


@job.command("list")
@click.option("--server", "-s", help="Server URL")
def job_list(server: str | None) -> None:
    """List all backup jobs."""
    import httpx

    server_url = server or "http://localhost:8420"

    try:
        response = httpx.get(f"{server_url}/api/v1/jobs")
        response.raise_for_status()
        jobs = response.json()

        if not jobs:
            console.print("No jobs configured")
            return

        table = Table(title="Backup Jobs")
        table.add_column("Name", style="cyan")
        table.add_column("Backend")
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
                j.get("backend", "rclone"),
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
@click.option("--backend", "-b", default="rclone", help="Backend to use")
@click.option("--schedule", help="Cron schedule (e.g., '0 2 * * *')")
@click.option("--server", help="Server URL")
def job_create(
    name: str,
    source: str,
    dest: str,
    backend: str,
    schedule: str | None,
    server: str | None,
) -> None:
    """Create a new backup job."""
    import httpx

    server_url = server or "http://localhost:8420"

    job_data = {
        "name": name,
        "source_path": source,
        "destination_path": dest,
        "backend": backend,
        "schedule_cron": schedule,
    }

    try:
        response = httpx.post(f"{server_url}/api/v1/jobs", json=job_data)
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
@click.argument("name")
@click.option("--dry-run", "-n", is_flag=True, help="Simulate without making changes")
@click.option("--server", help="Server URL")
def job_run(name: str, dry_run: bool, server: str | None) -> None:
    """Run a backup job."""
    import httpx

    server_url = server or "http://localhost:8420"

    try:
        response = httpx.post(
            f"{server_url}/api/v1/jobs/{name}/run",
            json={"dry_run": dry_run},
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
