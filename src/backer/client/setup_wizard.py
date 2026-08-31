"""Interactive setup wizard for Backer agent.

Provides a user-friendly terminal UI for configuring the agent,
connecting to a server, and registering automatically.
"""

import socket
import sys
from typing import Any

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, Prompt
from rich.table import Table

from backer import __version__
from backer.core.config import ClientConfig, load_config
from backer.core.paths import get_config_dir

console = Console()


def print_header() -> None:
    """Print the setup wizard header."""
    console.print()
    console.print(
        Panel.fit(
            f"[bold blue]Backer Agent Setup Wizard[/bold blue]\n[dim]Version {__version__}[/dim]",
            border_style="blue",
        )
    )
    console.print()


def check_existing_config() -> dict[str, Any] | None:
    """Check if agent is already configured."""
    config_path = get_config_dir() / "config.yaml"
    if config_path.exists():
        try:
            config = load_config(config_path)
            return config.server.model_dump() if config.server else None
        except Exception:
            pass
    return None


def test_server_connection(url: str) -> tuple[bool, str]:
    """Test connection to a Backer server.

    Returns:
        Tuple of (success, message)
    """
    try:
        # Normalize URL
        if not url.startswith("http"):
            url = f"http://{url}"
        url = url.rstrip("/")

        # Add port if missing
        if ":" not in url.split("//")[1]:
            url = f"{url}:8420"

        response = httpx.get(f"{url}/health", timeout=10.0)
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "ok":
                server_version = data.get("version", "unknown")
                return True, f"Connected! Server version: {server_version}"
        return False, f"Unexpected response: {response.status_code}"
    except httpx.ConnectError:
        return False, "Could not connect - check the URL and ensure server is running"
    except httpx.TimeoutException:
        return False, "Connection timed out"
    except Exception as e:
        return False, str(e)


def register_agent(server_url: str, enrollment_token: str) -> tuple[bool, str, dict[str, Any] | None]:
    """Register this machine as an agent with the server.

    Returns:
        Tuple of (success, message, credentials_dict)
    """
    try:
        hostname = socket.gethostname()
        import platform

        os_info = f"{platform.system()} {platform.release()}"

        response = httpx.post(
            f"{server_url}/api/v1/clients/register",
            json={
                "hostname": hostname,
                "version": __version__,
                "os_info": os_info,
                "tags": [],
                "enrollment_token": enrollment_token,
            },
            timeout=30.0,
        )

        if response.status_code == 200:
            data = response.json()
            return (
                True,
                "Agent registered successfully!",
                {
                    "server_url": server_url,
                    "client_id": data["client_id"],
                    "client_secret": data["client_secret"],
                },
            )
        else:
            return False, f"Registration failed: {response.text}", None

    except Exception as e:
        return False, f"Registration error: {e}", None


def save_config(config: dict[str, Any]) -> bool:
    """Save agent configuration."""
    try:
        config_path = get_config_dir() / "config.yaml"
        unified = load_config(config_path)
        unified.agent_id = config.get("client_id") or unified.agent_id
        unified.server = ClientConfig.model_validate(config)
        unified.save(config_path)

        return True
    except Exception as e:
        console.print(f"[red]Failed to save config: {e}[/red]")
        return False


def run_wizard() -> bool:
    """Run the interactive setup wizard.

    Returns:
        True if setup was successful
    """
    print_header()

    # Check for existing config
    existing = check_existing_config()
    if existing:
        console.print("[yellow]Existing configuration found:[/yellow]")
        table = Table(show_header=False, box=None)
        table.add_column("Key", style="dim")
        table.add_column("Value")
        table.add_row("Server", existing.get("server_url", "Not set"))
        table.add_row("Client ID", existing.get("client_id", "Not set"))
        console.print(table)
        console.print()

        if not Confirm.ask("Would you like to reconfigure?", default=False):
            console.print("[green]Using existing configuration.[/green]")
            return True
        console.print()

    # Step 1: Find or enter server URL
    console.print("[bold]Step 1:[/bold] Connect to Backer Server")
    console.print("[dim]The agent needs to connect to a Backer server to receive backup jobs.[/dim]")
    console.print()

    # Directly prompt for server URL (skip auto-discovery)
    server_url = Prompt.ask("Enter your Backer server URL", default="http://localhost:8420")

    # Normalize URL
    if not server_url.startswith("http"):
        server_url = f"http://{server_url}"
    server_url = server_url.rstrip("/")
    if ":" not in server_url.split("//")[1]:
        server_url = f"{server_url}:8420"

    # Test connection
    console.print()
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Testing connection...", total=None)
        success, message = test_server_connection(server_url)

    if not success:
        console.print(f"[red]Connection failed: {message}[/red]")
        if Confirm.ask("Try a different URL?", default=True):
            return run_wizard()  # Restart wizard
        return False

    console.print(f"[green]{message}[/green]")
    console.print()

    # Step 2: Register agent
    console.print("[bold]Step 2:[/bold] Register Agent")
    console.print(f"[dim]Registering this machine ({socket.gethostname()}) with the server.[/dim]")
    enrollment_token = Prompt.ask("Enter the enrollment key from the Agents page", password=True)
    console.print()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Registering agent...", total=None)
        success, message, credentials = register_agent(server_url, enrollment_token)

    if not success:
        console.print(f"[red]{message}[/red]")
        return False

    console.print(f"[green]{message}[/green]")
    console.print()

    # Save configuration
    console.print("[bold]Step 3:[/bold] Save Configuration")

    if credentials and save_config(credentials):
        config_path = get_config_dir() / "config.yaml"
        console.print(f"[green]Configuration saved to {config_path}[/green]")
    else:
        return False

    # Success!
    console.print()
    console.print(
        Panel.fit(
            "[bold green]Setup Complete![/bold green]\n\n"
            f"Server: {server_url}\n"
            f"Client ID: {credentials['client_id']}\n\n"
            "[dim]The agent is now configured and ready to run.[/dim]",
            border_style="green",
        )
    )
    console.print()

    console.print("[bold]Next Steps:[/bold]")
    console.print()

    if sys.platform == "win32":
        console.print("  Start the agent:")
        console.print("    [cyan]backer agent start[/cyan]")
    else:
        console.print("  Start the agent service:")
        console.print("    [cyan]sudo systemctl start backer-agent[/cyan]")
        console.print()
        console.print("  Enable on boot:")
        console.print("    [cyan]sudo systemctl enable backer-agent[/cyan]")

    console.print()
    console.print("  View agent status:")
    console.print("    [cyan]backer agent status[/cyan]")
    console.print()
    console.print("  View logs:")
    console.print("    [cyan]backer agent logs[/cyan]")
    console.print()

    return True


def main() -> int:
    """Entry point for the setup wizard."""
    try:
        success = run_wizard()
        return 0 if success else 1
    except KeyboardInterrupt:
        console.print("\n[yellow]Setup cancelled.[/yellow]")
        return 1
    except Exception as e:
        console.print(f"\n[red]Error: {e}[/red]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
