"""
Backer Agent Service

Background service that polls the server for commands and executes backups.
"""

import base64
import json
import logging
import ntpath
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from backer import __version__

# Explicitly import backend modules for PyInstaller
# PyInstaller's hiddenimports doesn't work reliably with dynamic __import__()
# These imports ensure the backends are registered before the registry is used
# Import each individually so one failure doesn't prevent others from loading
_backend_logger = logging.getLogger(__name__)
for _backend in ['kopia', 'proxy', 'rclone', 'restic']:
    try:
        __import__(f'backer.backends.{_backend}')
        _backend_logger.info(f"Backend '{_backend}' loaded successfully")
    except ImportError as e:
        _backend_logger.warning(f"Backend '{_backend}' failed to load: {e}")
    except Exception as e:
        _backend_logger.error(f"Backend '{_backend}' error: {e}", exc_info=True)


def get_subprocess_flags() -> int:
    """Get subprocess creation flags to hide console window on Windows."""
    if sys.platform == 'win32':
        # CREATE_NO_WINDOW = 0x08000000
        # This prevents the console window from appearing
        return subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0x08000000
    return 0


def setup_agent_logging(log_dir: Path | None = None) -> Path:
    """Configure logging to both console and file for the agent.

    Returns the log file path.
    """
    # Determine log directory
    if log_dir is None:
        if sys.platform == 'win32':
            log_dir = Path(os.environ.get('APPDATA', Path.home())) / 'Backer' / 'logs'
        else:
            log_dir = Path.home() / '.local' / 'share' / 'backer' / 'logs'

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"backer-agent-{datetime.now().strftime('%Y-%m-%d')}.log"

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # Clear any existing handlers
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%H:%M:%S"
    )
    console_handler.setFormatter(console_format)
    root_logger.addHandler(console_handler)

    # File handler with more detail
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_format)
    root_logger.addHandler(file_handler)

    return log_file


logger = logging.getLogger(__name__)


class SMBConnectionManager:
    """Manages persistent SMB connections for Windows agents.

    This prevents Error 1219 by reusing existing connections and properly
    managing credentials through Windows Credential Manager.
    """

    def __init__(self):
        self._connections: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def connect(
        self,
        server: str,
        share: str,
        username: str | None,
        password: str | None,
        domain: str | None = None,
    ) -> bool:
        """Connect to SMB share, reusing existing connection if credentials match.

        Args:
            server: SMB server hostname or IP
            share: Share name
            username: Username for authentication
            password: Password for authentication
            domain: Windows domain (optional)

        Returns:
            True if connection successful or already connected, False otherwise
        """
        key = (server, share)

        with self._lock:
            # Check if already connected with same credentials
            if key in self._connections:
                existing = self._connections[key]
                if existing['username'] == username:
                    logger.info(f"[SMB-POOL] Reusing existing connection to {server}/{share}")
                    return True
                else:
                    # Different credentials - need to reconnect
                    logger.warning(
                        f"[SMB-POOL] Credential change detected for {server}/{share}, "
                        f"reconnecting..."
                    )
                    self._disconnect_internal(server, share)

            # Check for conflicts with other shares on the same server
            conflict = self._find_server_conflict(server, username)
            if conflict:
                logger.error(
                    f"[SMB-POOL] Cannot connect to {server}/{share} - "
                    f"existing connection found: {conflict}. "
                    f"Windows only allows one credential set per server."
                )
                return False

            # Store credentials in Credential Manager first
            if username and password:
                if not self._store_credentials(server, username, password, domain):
                    logger.warning("[SMB-POOL] Failed to store credentials; trying explicit net use credentials")
                    return self._connect_with_explicit_credentials(server, share, username, password, domain)

            # Attempt connection
            unc_path = f"\\\\{server}\\{share}"
            cmd = ['net', 'use', unc_path, '/persistent:no']

            logger.debug(f"[SMB-POOL] Connecting to {unc_path}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                creationflags=get_subprocess_flags()
            )

            if result.returncode == 0:
                self._connections[key] = {
                    'username': username,
                    'connected_at': datetime.now(),
                    'server': server,
                    'share': share,
                }
                logger.info(f"[SMB-POOL] Successfully connected to {unc_path}")
                return True

            # Handle Error 1219 - connection already exists with different credentials
            if '1219' in result.stderr:
                existing_conn = self._find_existing_connection(server)
                if existing_conn:
                    logger.error(
                        f"[SMB-POOL] Error 1219: Cannot connect to {unc_path}.\n"
                        f"Existing connection: {existing_conn}\n"
                        f"Please disconnect the existing connection or use the same credentials."
                    )
                else:
                    logger.error("[SMB-POOL] Error 1219 detected but couldn't identify conflicting connection")
                return False

            # Handle Error 1223 - operation canceled (UAC/permission issue)
            if '1223' in result.stderr:
                logger.error(
                    f"[SMB-POOL] Error 1223: Operation canceled connecting to {unc_path}. "
                    f"This usually means:\n"
                    f"  1. Agent is not running with Administrator privileges\n"
                    f"  2. UAC is blocking the operation\n"
                    f"  3. Windows security policy is preventing credential storage\n"
                    f"Attempting connection with explicit credentials as fallback..."
                )
                # Try direct connection with explicit credentials
                return self._connect_with_explicit_credentials(server, share, username, password, domain)

            logger.error(f"[SMB-POOL] Connection failed: {result.stderr}")
            return False

    def _find_server_conflict(self, server: str, username: str | None) -> str | None:
        """Check if there's a conflicting connection to this server.

        Returns the conflicting share name if found, None otherwise.
        """
        for (conn_server, conn_share), info in self._connections.items():
            if conn_server.lower() == server.lower() and info['username'] != username:
                return f"\\\\{conn_server}\\{conn_share} (user: {info['username']})"
        return None

    def _find_existing_connection(self, server: str) -> str | None:
        """Find existing net use connection to server.

        Returns the connection string if found, None otherwise.
        """
        try:
            result = subprocess.run(
                ['net', 'use'],
                capture_output=True,
                text=True,
                creationflags=get_subprocess_flags()
            )

            for line in result.stdout.split('\n'):
                if server.lower() in line.lower() and '\\\\' in line:
                    return line.strip()
        except Exception as e:
            logger.debug(f"[SMB-POOL] Error checking existing connections: {e}")

        return None

    def _store_credentials(
        self,
        server: str,
        username: str,
        password: str,
        domain: str | None
    ) -> bool:
        """Store credentials in Windows Credential Manager.

        Returns True if successful, False otherwise.
        """
        full_user = f"{domain}\\{username}" if domain else username

        # Delete any existing cached credentials for this server
        subprocess.run(
            ['cmdkey', '/delete', f'\\\\{server}'],
            capture_output=True,
            creationflags=get_subprocess_flags(),
        )

        # Add new credentials
        result = subprocess.run(
            ['cmdkey', '/add', f'\\\\{server}', '/user', full_user, '/pass', password],
            capture_output=True,
            text=True,
            creationflags=get_subprocess_flags(),
        )

        if result.returncode != 0:
            logger.debug(f"[SMB-POOL] cmdkey add failed: {result.stderr}")
            return False

        return True

    def _connect_with_explicit_credentials(
        self,
        server: str,
        share: str,
        username: str,
        password: str,
        domain: str | None = None,
    ) -> bool:
        """Fallback method to connect using explicit credentials in net use command.

        This bypasses cmdkey and passes credentials directly to net use.
        Used when Error 1223 occurs (UAC/permission issues preventing credential storage).

        Args:
            server: SMB server hostname or IP
            share: Share name
            username: Username for authentication
            password: Password for authentication
            domain: Optional domain name

        Returns:
            True if connection successful, False otherwise
        """
        unc_path = f"\\\\{server}\\{share}"
        full_user = f"{domain}\\{username}" if domain else username

        logger.info(f"[SMB-POOL] Attempting explicit credential connection to {unc_path}")

        # Try to connect with explicit credentials
        result = subprocess.run(
            ['net', 'use', unc_path, f'/user:{full_user}', password],
            capture_output=True,
            text=True,
            creationflags=get_subprocess_flags(),
        )

        if result.returncode != 0:
            logger.error(f"[SMB-POOL] Explicit credential connection failed: {result.stderr}")
            return False

        # Track the connection
        key = (server, share)
        self._connections[key] = {
            'username': username,
            'domain': domain,
            'connected_at': datetime.now().isoformat(),
            'method': 'explicit',  # Mark as using explicit credentials
        }

        logger.info(f"[SMB-POOL] Successfully connected to {unc_path} using explicit credentials")
        return True

    def disconnect(self, server: str, share: str) -> None:
        """Disconnect from a specific SMB share.

        Args:
            server: SMB server hostname or IP
            share: Share name
        """
        with self._lock:
            self._disconnect_internal(server, share)

    def _disconnect_internal(self, server: str, share: str) -> None:
        """Internal disconnect without lock (caller must hold lock)."""
        key = (server, share)
        unc_path = f"\\\\{server}\\{share}"

        # Remove the connection
        subprocess.run(
            ['net', 'use', unc_path, '/delete', '/y'],
            capture_output=True,
            creationflags=get_subprocess_flags(),
        )

        # Clean up credentials if no other shares on this server
        has_other_shares = any(
            conn_server == server
            for (conn_server, conn_share) in self._connections.keys()
            if conn_share != share
        )

        if not has_other_shares:
            subprocess.run(
                ['cmdkey', '/delete', f'\\\\{server}'],
                capture_output=True,
                creationflags=get_subprocess_flags(),
            )

        # Remove from tracking
        self._connections.pop(key, None)
        logger.debug(f"[SMB-POOL] Disconnected from {unc_path}")

    def disconnect_all(self) -> None:
        """Disconnect all managed connections (cleanup on shutdown)."""
        with self._lock:
            for (server, share) in list(self._connections.keys()):
                self._disconnect_internal(server, share)
            logger.info("[SMB-POOL] All connections disconnected")

    def get_connection_status(self) -> dict[str, Any]:
        """Get current connection status for monitoring.

        Returns:
            Dictionary with connection information
        """
        with self._lock:
            return {
                'active_connections': len(self._connections),
                'connections': [
                    {
                        'server': conn['server'],
                        'share': conn['share'],
                        'username': conn['username'],
                        'connected_at': conn['connected_at'].isoformat(),
                    }
                    for conn in self._connections.values()
                ]
            }


class AgentService:
    """Background service that communicates with the Backer server."""

    def __init__(
        self,
        server_url: str,
        client_id: str,
        client_secret: str,
        tools_dir: Path | None = None,
        poll_interval: int = 10,
        status_callback: Callable[[str], None] | None = None,
    ):
        self.server_url = server_url.rstrip('/')
        self.client_id = client_id
        self.client_secret = client_secret
        self.poll_interval = poll_interval
        self.status_callback = status_callback

        # Find tools directory
        if tools_dir:
            self.tools_dir = tools_dir
        else:
            # Default locations
            if sys.platform == 'win32':
                # Check in app directory first, then AppData
                if getattr(sys, 'frozen', False):
                    app_dir = Path(sys.executable).parent
                    if (app_dir / 'tools' / 'rclone.exe').exists():
                        self.tools_dir = app_dir / 'tools'
                    else:
                        self.tools_dir = Path(os.environ.get('APPDATA', '')) / 'Backer' / 'tools'
                else:
                    self.tools_dir = Path(os.environ.get('APPDATA', '')) / 'Backer' / 'tools'
            else:
                self.tools_dir = Path.home() / '.local' / 'share' / 'backer' / 'tools'

        # Ensure tools directory exists
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault('BACKER_DATA_DIR', str(self.tools_dir.parent))
        logger.info(f"Tools directory: {self.tools_dir}")

        # Initialize tool manager for automatic tool downloads
        self._tool_manager = None
        self._tools_ready = False

        # Initialize SMB connection pool for Windows
        self._smb_manager = SMBConnectionManager() if sys.platform == 'win32' else None

        self._running = False
        self._thread: threading.Thread | None = None

    def ensure_tools_installed(self, progress_callback: Callable[[str], None] | None = None) -> dict[str, bool]:
        """Ensure backup tools (rclone, restic) are installed.

        Downloads tools automatically if not present.

        Args:
            progress_callback: Optional callback for progress updates

        Returns:
            Dict with tool names and whether they're ready
        """
        from backer.tools.manager import ToolManager

        logger.info("[TOOLS] Checking backup tools installation...")

        if self._tool_manager is None:
            self._tool_manager = ToolManager(self.tools_dir)

        results = {}

        # List of required tools
        required_tools = ['rclone', 'restic', 'kopia']

        for tool_name in required_tools:
            try:
                logger.info(f"[TOOLS] Checking {tool_name}...")

                if progress_callback:
                    progress_callback(f"Checking {tool_name}...")

                # Check if already installed
                tool_path = self._tool_manager.get_tool_path(tool_name)

                if tool_path:
                    logger.info(f"[TOOLS] {tool_name} found at: {tool_path}")
                    results[tool_name] = True
                else:
                    # Need to download
                    logger.info(f"[TOOLS] {tool_name} not found, downloading...")

                    if progress_callback:
                        progress_callback(f"Downloading {tool_name}...")

                    def download_progress(msg: str):
                        logger.info(f"[TOOLS] {msg}")
                        if progress_callback:
                            progress_callback(msg)

                    try:
                        path = self._tool_manager.download(tool_name, progress_callback=download_progress)
                        logger.info(f"[TOOLS] {tool_name} installed to: {path}")
                        results[tool_name] = True

                        if progress_callback:
                            progress_callback(f"{tool_name} installed successfully")

                    except Exception as download_err:
                        logger.error(f"[TOOLS] Failed to download {tool_name}: {download_err}")
                        results[tool_name] = False

                        if progress_callback:
                            progress_callback(f"Failed to download {tool_name}: {download_err}")

            except Exception as e:
                logger.error(f"[TOOLS] Error checking {tool_name}: {e}")
                results[tool_name] = False

        # Update tools ready flag
        self._tools_ready = all(results.values())

        if self._tools_ready:
            logger.info("[TOOLS] All backup tools are ready")
        else:
            failed = [t for t, ready in results.items() if not ready]
            logger.warning(f"[TOOLS] Some tools failed to install: {failed}")

        return results

    def get_tool_status(self) -> dict[str, dict]:
        """Get detailed status of all backup tools."""
        from backer.tools.manager import ToolManager

        if self._tool_manager is None:
            self._tool_manager = ToolManager(self.tools_dir)

        return self._tool_manager.list_tools()

    def get_smb_status(self) -> dict[str, Any]:
        """Get SMB connection pool status for monitoring and diagnostics.

        Returns:
            Dictionary with SMB connection information, or status indicating
            SMB management is not available on this platform.
        """
        if not self._smb_manager:
            return {
                'available': False,
                'platform': sys.platform,
                'message': 'SMB connection pooling not available on this platform'
            }

        status = self._smb_manager.get_connection_status()
        status['available'] = True
        status['platform'] = sys.platform
        return status

    def _get_auth_header(self) -> str:
        """Get HTTP Basic auth header."""
        credentials = f"{self.client_id}:{self.client_secret}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"

    def _make_request(
        self,
        endpoint: str,
        method: str = 'GET',
        data: dict | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        """Make authenticated request to server."""
        url = f"{self.server_url}{endpoint}"

        if data is not None:
            body = json.dumps(data).encode('utf-8')
        else:
            body = None

        req = urllib.request.Request(url, data=body, method=method)
        req.add_header('Authorization', self._get_auth_header())
        req.add_header('Content-Type', 'application/json')
        req.add_header('User-Agent', f'Backer-Agent/{__version__}')

        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))

    def _update_status(self, status: str):
        """Update status via callback if available."""
        if self.status_callback:
            self.status_callback(status)
        logger.info(f"Agent status: {status}")

    def start(self):
        """Start the background service."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Agent service started")

    def stop(self):
        """Stop the background service."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

        # Cleanup SMB connections
        if self._smb_manager:
            self._smb_manager.disconnect_all()

        logger.info("Agent service stopped")

    def _run_loop(self):
        """Main service loop.

        Uses long-polling: the server holds the heartbeat request until
        a command is available (up to 25s), so we get commands instantly.
        We only sleep briefly between heartbeats to allow quick shutdown.
        """
        while self._running:
            try:
                self._heartbeat()
            except Exception as e:
                logger.error(f"Heartbeat failed: {e}")
                self._update_status(f"Error: {e}")
                # On error, wait before retry to avoid hammering server
                for _ in range(5):
                    if not self._running:
                        break
                    time.sleep(1)
                continue

            # Brief pause between heartbeats (server handles the waiting)
            if not self._running:
                break
            time.sleep(1)

    def _heartbeat(self):
        """Send heartbeat to server and process any commands.

        Uses long-polling with 35 second timeout - the server holds the
        connection until a command arrives (up to 25 seconds).
        """
        self._update_status("Checking for jobs...")

        try:
            response = self._make_request(
                '/api/v1/clients/heartbeat',
                method='POST',
                data={
                    'client_id': self.client_id,
                    'status': 'online',
                },
                timeout=35,  # Server waits up to 25s, so we need longer timeout
            )

            commands = response.get('commands', [])
            if commands:
                self._update_status(f"Processing {len(commands)} command(s)...")
                for cmd in commands:
                    self._process_command(cmd)
            else:
                self._update_status("Connected - Idle")

        except urllib.error.HTTPError as e:
            if e.code == 401:
                self._update_status("Authentication failed - re-register required")
            else:
                raise

    def _redact_sensitive_data(self, data: Any) -> Any:
        """Return a safe-to-log copy of command data."""
        sensitive_parts = (
            'password', 'token', 'secret', 'access_key', 'api_key', 'private_key',
            'authorization', 'credential', 'capability',
        )
        if isinstance(data, dict):
            return {
                key: '***REDACTED***' if any(part in str(key).lower() for part in sensitive_parts)
                else self._redact_sensitive_data(value)
                for key, value in data.items()
            }
        if isinstance(data, list):
            return [self._redact_sensitive_data(value) for value in data]
        if isinstance(data, tuple):
            return tuple(self._redact_sensitive_data(value) for value in data)
        return data

    def _process_command(self, command: dict[str, Any]):
        """Process a command from the server."""
        cmd_id = command.get('id')
        cmd_type = command.get('command_type')
        payload = command.get('payload', {})

        logger.info(f"[COMMAND] Processing command {cmd_id}: {cmd_type}")
        # Redact sensitive data before logging
        safe_payload = self._redact_sensitive_data(payload)
        logger.debug(f"[COMMAND] Full payload: {json.dumps(safe_payload, indent=2)}")

        try:
            if cmd_type == 'backup':
                # Use retry logic for backups (SMB/network failures are common)
                self._execute_backup_with_retry(payload, max_retries=3)
            elif cmd_type == 'restore':
                self._execute_restore(payload)
            elif cmd_type == 'browse_filesystem':
                self._execute_browse_filesystem(payload)
            else:
                logger.warning(f"[COMMAND] Unknown command type: {cmd_type}")

            # Acknowledge command
            logger.debug(f"[COMMAND] Acknowledging command {cmd_id}")
            self._make_request(f'/api/v1/commands/{cmd_id}/ack', method='POST')
            logger.info(f"[COMMAND] Command {cmd_id} completed successfully")

        except Exception as e:
            logger.error(f"[COMMAND] Command {cmd_id} failed: {e}")
            logger.error(f"[COMMAND] Traceback:\n{traceback.format_exc()}")
            # Report failure
            self._report_result(
                run_id=payload.get('run_id', 'unknown'),
                job_name=payload.get('job_name', 'unknown'),
                success=False,
                error=str(e),
            )

    def _execute_backup_with_retry(self, payload: dict[str, Any], max_retries: int = 3):
        """Execute backup with retry logic for transient failures.

        Args:
            payload: Backup command payload
            max_retries: Maximum number of retry attempts (default: 3)

        Raises:
            Exception: If all retry attempts fail
        """
        last_error = None
        job_name = payload.get('job_name', 'unknown')

        for attempt in range(max_retries):
            try:
                logger.info(f"[BACKUP] Attempt {attempt + 1}/{max_retries} for job '{job_name}'")
                if not self._execute_backup(payload):
                    raise RuntimeError(f"Backup failed: {job_name}")
                logger.info(f"[BACKUP] Job '{job_name}' completed successfully on attempt {attempt + 1}")
                return  # Success - exit retry loop

            except Exception as e:
                last_error = str(e)
                error_lower = last_error.lower()

                # Check if error is retryable
                is_retryable = any(keyword in error_lower for keyword in [
                    '1219',  # Windows SMB error
                    'smb',
                    'network',
                    'connection',
                    'timeout',
                    'unreachable',
                    'refused',
                    'failed to connect',
                ])

                if is_retryable and attempt < max_retries - 1:
                    delay = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                    logger.warning(
                        f"[BACKUP] Attempt {attempt + 1}/{max_retries} failed with retryable error: {last_error}. "
                        f"Retrying in {delay}s..."
                    )
                    time.sleep(delay)
                    continue
                else:
                    # Non-retryable error or final attempt
                    if attempt < max_retries - 1:
                        logger.error(f"[BACKUP] Non-retryable error encountered: {last_error}")
                    raise

        # If we get here, all attempts failed
        raise RuntimeError(f"Backup failed after {max_retries} attempts. Last error: {last_error}")

    def _execute_with_shared_agent(self, payload: dict[str, Any], restore: bool) -> Any:
        """Use the CLI agent executor for GUI/service commands too."""
        from backer.client.agent import BackerAgent

        executor = BackerAgent(self.server_url, self.client_id, self.client_secret)
        if restore:
            return executor.execute_restore(payload, dry_run=payload.get('dry_run', False))
        report = executor.execute_backup(payload, dry_run=payload.get('dry_run', False))
        return report['success']

    def _execute_backup(self, payload: dict[str, Any]):
        """Execute a backup command."""
        return self._execute_with_shared_agent(payload, restore=False)

    def _execute_restore(self, payload: dict[str, Any]):
        """Execute a restore command."""
        return self._execute_with_shared_agent(payload, restore=True)

    def _get_tool_path(self, tool_name: str) -> Path:
        """Get path to a backup tool, using tool manager if available."""
        from backer.tools.manager import ToolManager

        if self._tool_manager is None:
            self._tool_manager = ToolManager(self.tools_dir)

        tool_path = self._tool_manager.get_tool_path(tool_name)

        if tool_path:
            return tool_path

        # Fallback to direct path in tools dir
        if sys.platform == 'win32':
            binary = self.tools_dir / f'{tool_name}.exe'
        else:
            binary = self.tools_dir / tool_name

        if binary.exists():
            return binary

        # Download only the executable required by the queued job.
        return self._tool_manager.download(tool_name)

    def _get_restic_snapshot_paths(
        self,
        restic: Path,
        repo: str,
        snapshot_id: str,
        env: dict[str, str],
    ) -> list[str]:
        """Get the original paths from a restic snapshot."""
        try:
            cmd = [str(restic), '-r', repo, 'snapshots', snapshot_id, '--json']
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
                creationflags=get_subprocess_flags(),
            )
            if result.returncode == 0 and result.stdout:
                snapshots = json.loads(result.stdout)
                if snapshots and len(snapshots) > 0:
                    # Return the paths from the snapshot
                    return snapshots[0].get('paths', [])
        except Exception as e:
            logger.warning(f"[RESTIC] Could not query snapshot paths: {e}")
        return []

    def _normalize_windows_path(self, path: str | None) -> str:
        """Normalize path for Windows - convert forward slashes to backslashes for UNC paths.

        On Windows, UNC paths must use backslashes (\\\\server\\share), but our server
        stores them with forward slashes (//server/share) for cross-platform compatibility.
        """
        if path is None:
            raise ValueError("Path cannot be None - job may be missing destination_path")
        if sys.platform == 'win32':
            # Convert forward slash UNC paths to backslash format
            if path.startswith('//'):
                return path.replace('/', '\\')
        return path

    def _ensure_repository_parent_directory(self, repo_path: str) -> None:
        """Create the parent directory for filesystem-backed repositories."""
        if not repo_path or "://" in repo_path:
            return

        stripped = repo_path.rstrip("\\/")
        if not stripped:
            return

        if sys.platform == 'win32':
            parent = ntpath.dirname(stripped)
            if not parent or parent == stripped:
                return
            logger.info(f"[BACKUP] Ensuring repository parent directory exists: {parent}")
            Path(parent).mkdir(parents=True, exist_ok=True)
            return

        path = Path(stripped)
        if not path.is_absolute():
            return
        logger.info(f"[BACKUP] Ensuring repository parent directory exists: {path.parent}")
        path.parent.mkdir(parents=True, exist_ok=True)

    def _connect_windows_smb(
        self,
        server: str,
        share: str,
        username: str | None = None,
        password: str | None = None,
        domain: str | None = None,
    ) -> bool:
        """Connect to an SMB share on Windows using the connection pool.

        This establishes credentials for accessing the share so that
        tools like kopia can access UNC paths without authentication errors.

        Uses the SMB connection pool to prevent Error 1219 by reusing
        existing connections when credentials match.

        Returns True if connection successful or already connected.
        """
        if sys.platform != 'win32':
            return True  # Not needed on Linux (we mount instead)

        if not self._smb_manager:
            logger.error("[SMB] Connection pool not initialized")
            return False

        logger.info(f"[SMB] Requesting connection to \\\\{server}\\{share}")
        return self._smb_manager.connect(server, share, username, password, domain)

    def _disconnect_windows_smb(self, server: str, share: str) -> None:
        """Disconnect from an SMB share on Windows.

        Note: With connection pooling, this is typically not called per-backup
        as connections are reused. Cleanup happens on agent shutdown.
        """
        if sys.platform != 'win32':
            return

        if self._smb_manager:
            logger.debug(f"[SMB] Requesting disconnect from \\\\{server}\\{share}")
            self._smb_manager.disconnect(server, share)

    def _run_rclone_sync(
        self,
        source: str,
        dest: str,
        excludes: list[str],
        dry_run: bool,
        run_id: str,
    ) -> dict[str, Any]:
        """Run rclone sync command."""
        logger.info("[RCLONE] Setting up rclone sync")

        # Normalize paths for Windows (convert UNC forward slashes to backslashes)
        source = self._normalize_windows_path(source)
        dest = self._normalize_windows_path(dest)

        logger.debug(f"[RCLONE] Source: {source}")
        logger.debug(f"[RCLONE] Destination: {dest}")

        try:
            rclone = self._get_tool_path('rclone')
            logger.info(f"[RCLONE] Using rclone at: {rclone}")
        except FileNotFoundError as e:
            logger.error(f"[RCLONE] {e}")
            logger.info(f"[RCLONE] Tools directory: {self.tools_dir}")
            if self.tools_dir.exists():
                logger.info(f"[RCLONE] Tools directory contents: {list(self.tools_dir.iterdir())}")
            raise

        cmd = [str(rclone), 'sync', source, dest, '--progress', '-v']

        for exclude in excludes:
            cmd.extend(['--exclude', exclude])

        if dry_run:
            cmd.append('--dry-run')

        logger.info(f"[RCLONE] Executing command: {' '.join(cmd)}")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=get_subprocess_flags(),
        )

        output_lines = []
        bytes_transferred = 0
        files_transferred = 0

        for line in process.stdout:
            output_lines.append(line)
            logger.debug(line.strip())

            # Parse progress from rclone output
            if 'Transferred:' in line and 'ETA' in line:
                # Try to extract progress percentage
                try:
                    if '%' in line:
                        parts = line.split('%')[0].split()
                        percent = int(parts[-1])
                        self._report_progress(run_id, 'running', percent, line.strip())
                except (ValueError, IndexError):
                    pass

        try:
            process.wait(timeout=300)  # 5 minute timeout for process cleanup
        except subprocess.TimeoutExpired:
            logger.warning("[RCLONE] Process wait timed out, killing process")
            process.kill()
            process.wait(timeout=10)
        output = ''.join(output_lines)

        logger.info(f"[RCLONE] Process completed with return code: {process.returncode}")
        if process.returncode != 0:
            logger.error(f"[RCLONE] Process failed! Output:\n{output}")
        else:
            logger.info("[RCLONE] Sync completed successfully")

        return {
            'success': process.returncode == 0,
            'output': output,
            'bytes': bytes_transferred,
            'files': files_transferred,
            'error': None if process.returncode == 0 else f"Exit code: {process.returncode}",
        }

    def _init_restic_repo(self, restic: Path, dest: str, env: dict) -> bool:
        """Initialize restic repository if it doesn't exist."""
        # Check if repo exists by running snapshots command
        check_cmd = [str(restic), '-r', dest, 'snapshots', '--json']
        logger.info("[RESTIC] Checking if repository exists...")

        result = subprocess.run(
            check_cmd,
            capture_output=True,
            text=True,
            env=env,
            creationflags=get_subprocess_flags(),
        )

        if result.returncode == 0:
            logger.info("[RESTIC] Repository already exists")
            return True

        # Repository doesn't exist, initialize it
        logger.info("[RESTIC] Repository not found, initializing...")
        try:
            self._ensure_repository_parent_directory(dest)
        except OSError as exc:
            logger.error(f"[RESTIC] Failed to create repository parent directory: {exc}")
            return False

        init_cmd = [str(restic), '-r', dest, 'init']

        result = subprocess.run(
            init_cmd,
            capture_output=True,
            text=True,
            env=env,
            creationflags=get_subprocess_flags(),
        )

        if result.returncode == 0:
            logger.info("[RESTIC] Repository initialized successfully")
            return True
        else:
            logger.error(f"[RESTIC] Failed to initialize repository: {result.stderr}")
            return False

    def _run_restic_backup(
        self,
        source: str,
        dest: str,
        excludes: list[str],
        dry_run: bool,
        run_id: str,
        backend_options: dict[str, Any] | None = None,
        smb_server: str | None = None,
        smb_share: str | None = None,
        smb_username: str | None = None,
        smb_password: str | None = None,
        smb_domain: str | None = None,
    ) -> dict[str, Any]:
        """Run restic backup command."""
        logger.info("[RESTIC] Setting up restic backup")

        # Normalize paths for Windows (convert UNC forward slashes to backslashes)
        source = self._normalize_windows_path(source)
        dest = self._normalize_windows_path(dest)

        logger.debug(f"[RESTIC] Source: {source}")
        logger.debug(f"[RESTIC] Repository: {dest}")

        # On Windows, connect to SMB share with credentials if provided
        if sys.platform == 'win32' and smb_server and smb_share:
            if not self._connect_windows_smb(smb_server, smb_share, smb_username, smb_password, smb_domain):
                return {
                    'success': False,
                    'output': 'Failed to connect to SMB share',
                    'bytes': 0,
                    'files': 0,
                    'error': f'Failed to connect to \\\\{smb_server}\\{smb_share}',
                }

        try:
            restic = self._get_tool_path('restic')
            logger.info(f"[RESTIC] Using restic at: {restic}")
        except FileNotFoundError as e:
            logger.error(f"[RESTIC] {e}")
            raise

        backend_options = backend_options or {}

        # Set up environment with password
        # Priority: backend_options > env var > default
        env = os.environ.copy()
        if 'password' in backend_options:
            env['RESTIC_PASSWORD'] = backend_options['password']
            logger.info("[RESTIC] Using password from job configuration")
        elif 'restic_password' in backend_options:
            env['RESTIC_PASSWORD'] = backend_options['restic_password']
            logger.info("[RESTIC] Using restic_password from job configuration")
        elif 'RESTIC_PASSWORD' not in env:
            env['RESTIC_PASSWORD'] = 'backer-default-password'
            logger.info("[RESTIC] Using default repository password")

        # Initialize repository if needed
        if not self._init_restic_repo(restic, dest, env):
            return {
                'success': False,
                'output': 'Failed to initialize restic repository',
                'bytes': 0,
                'files': 0,
                'error': 'Repository initialization failed',
            }

        cmd = [str(restic), '-r', dest, 'backup', source, '--json']

        for exclude in excludes:
            cmd.extend(['--exclude', exclude])

        if dry_run:
            cmd.append('--dry-run')

        logger.info(f"[RESTIC] Executing command: {' '.join(cmd)}")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            creationflags=get_subprocess_flags(),
        )

        output_lines = []
        snapshot_id = None
        files_new = 0
        bytes_added = 0

        for line in process.stdout:
            output_lines.append(line)
            logger.debug(line.strip())

            # Parse JSON output for snapshot info
            try:
                data = json.loads(line)
                if data.get('message_type') == 'summary':
                    snapshot_id = data.get('snapshot_id')
                    files_new = data.get('files_new', 0)
                    bytes_added = data.get('data_added', 0)
                    logger.info(f"[RESTIC] Snapshot ID: {snapshot_id}")
                    logger.info(f"[RESTIC] Files new: {files_new}, Bytes added: {bytes_added}")
            except (json.JSONDecodeError, TypeError):
                pass  # Not all lines are JSON

        try:
            process.wait(timeout=300)  # 5 minute timeout for process cleanup
        except subprocess.TimeoutExpired:
            logger.warning("[RESTIC] Process wait timed out, killing process")
            process.kill()
            process.wait(timeout=10)
        output = ''.join(output_lines)

        logger.info(f"[RESTIC] Process completed with return code: {process.returncode}")
        if process.returncode != 0:
            logger.error(f"[RESTIC] Process failed! Output:\n{output}")
        else:
            logger.info("[RESTIC] Backup completed successfully")

        return {
            'success': process.returncode == 0,
            'output': output,
            'bytes': bytes_added,
            'files': files_new,
            'snapshot_id': snapshot_id,
            'error': None if process.returncode == 0 else f"Exit code: {process.returncode}",
        }

    def _run_restic_restore(
        self,
        repo: str,
        dest: str,
        snapshot: str | None,
        dry_run: bool,
        run_id: str,
        backend_options: dict[str, Any] | None = None,
        include_path: str | None = None,
    ) -> dict[str, Any]:
        """Run restic restore command."""
        logger.info("[RESTIC] Setting up restic restore")

        # Normalize paths for Windows (convert UNC forward slashes to backslashes)
        repo = self._normalize_windows_path(repo)
        dest = self._normalize_windows_path(dest)

        logger.debug(f"[RESTIC] Repository: {repo}")
        logger.debug(f"[RESTIC] Destination: {dest}")
        logger.debug(f"[RESTIC] Snapshot: {snapshot or 'latest'}")
        logger.debug(f"[RESTIC] Include path: {include_path or 'all'}")

        try:
            restic = self._get_tool_path('restic')
            logger.info(f"[RESTIC] Using restic at: {restic}")
        except FileNotFoundError as e:
            logger.error(f"[RESTIC] {e}")
            raise

        backend_options = backend_options or {}

        # Set up environment with password
        # Priority: backend_options > env var > default
        env = os.environ.copy()
        if 'restic_password' in backend_options:
            env['RESTIC_PASSWORD'] = backend_options['restic_password']
            logger.info("[RESTIC] Using password from job configuration")
        elif 'RESTIC_PASSWORD' not in env:
            env['RESTIC_PASSWORD'] = 'backer-default-password'
            logger.info("[RESTIC] Using default repository password")

        # Use provided snapshot or "latest"
        snapshot_id = snapshot if snapshot else 'latest'

        # Query the snapshot to get the original backup paths
        # Restic stores absolute paths, so we need to determine the correct target
        original_paths = self._get_restic_snapshot_paths(restic, repo, snapshot_id, env)
        logger.debug(f"[RESTIC] Original backup paths: {original_paths}")

        # Determine the actual restore target
        # Restic recreates the full path structure inside --target
        #
        # WINDOWS QUIRK: Restic stores Windows paths like C:\Test internally as /C/Test
        # (drive letter becomes a directory). When restoring with --target /, files
        # end up at \C\Test (current drive root + \C\Test), e.g., C:\C\Test instead of C:\Test.
        #
        # Solution for Windows: Restore to drive root, then move files from the nested
        # drive letter directory to the correct location.
        restore_target = dest
        needs_windows_path_fix = False
        windows_drive_letter = None
        original_dest = dest

        if original_paths:
            # Check if destination matches or is parent of original backup path
            dest_normalized = dest.replace('\\', '/').rstrip('/')
            for orig_path in original_paths:
                orig_normalized = orig_path.replace('\\', '/').rstrip('/')
                if dest_normalized == orig_normalized or orig_normalized.startswith(dest_normalized + '/'):
                    # Restoring to original location
                    if sys.platform == 'win32' and len(dest) >= 2 and dest[1] == ':':
                        # Windows absolute path - extract drive letter
                        windows_drive_letter = dest[0].upper()
                        # Restore to drive root - files will end up at <drive>:\<drive>\<path>
                        restore_target = f"{windows_drive_letter}:\\"
                        needs_windows_path_fix = True
                        logger.info(f"[RESTIC] Windows restore: using target {restore_target}, will fix path after")
                    else:
                        # Unix or relative path - use / as target
                        restore_target = '/'
                        logger.info(f"[RESTIC] Restoring to original location, using target: {restore_target}")
                    break

        logger.debug(f"[RESTIC] Final restore target: {restore_target}")

        # Build restore command
        # restic restore <snapshot> --target <dest>
        # Use -vv for more verbose output to debug restore issues
        cmd = [str(restic), '-r', repo, 'restore', snapshot_id, '--target', restore_target, '-vv']

        # Add --include to restore only specific path from snapshot
        if include_path:
            # Strip leading slashes to make it relative
            include_path = include_path.lstrip('/')
            cmd.extend(['--include', include_path])

        if dry_run:
            cmd.append('--dry-run')

        logger.info(f"[RESTIC] Executing command: {' '.join(cmd)}")
        logger.info(f"[RESTIC] Expected restore location: {dest}")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            creationflags=get_subprocess_flags(),
        )

        output_lines = []
        for line in process.stdout:
            output_lines.append(line)
            logger.debug(line.strip())

        try:
            process.wait(timeout=300)  # 5 minute timeout for process cleanup
        except subprocess.TimeoutExpired:
            logger.warning("[RESTIC] Restore process wait timed out, killing process")
            process.kill()
            process.wait(timeout=10)
        output = ''.join(output_lines)

        logger.info(f"[RESTIC] Process completed with return code: {process.returncode}")
        if process.returncode != 0:
            logger.error(f"[RESTIC] Process failed! Output:\n{output}")
        else:
            logger.info("[RESTIC] Restore completed successfully")

        # Windows path fix: move files from nested drive letter directory to correct location
        if needs_windows_path_fix and process.returncode == 0 and windows_drive_letter:
            # Files were restored to e.g., C:\C\Test instead of C:\Test
            # We need to move them from the nested location
            try:
                # Calculate the nested path where restic put the files
                # e.g., for dest=C:\Test, files are at C:\C\Test
                dest_without_drive = original_dest[2:].lstrip('\\').lstrip('/')  # "Test" from "C:\Test"
                nested_path = Path(f"{windows_drive_letter}:\\{windows_drive_letter}\\{dest_without_drive}")
                actual_dest = Path(original_dest)

                logger.info(f"[RESTIC] Windows path fix: moving from {nested_path} to {actual_dest}")

                if nested_path.exists():
                    import shutil

                    # Ensure destination parent exists
                    actual_dest.parent.mkdir(parents=True, exist_ok=True)

                    # If destination doesn't exist, just rename/move
                    if not actual_dest.exists():
                        shutil.move(str(nested_path), str(actual_dest))
                        logger.info(f"[RESTIC] Moved {nested_path} to {actual_dest}")
                    else:
                        # Destination exists, merge contents
                        for item in nested_path.iterdir():
                            dest_item = actual_dest / item.name
                            if dest_item.exists():
                                if dest_item.is_dir():
                                    shutil.rmtree(str(dest_item))
                                else:
                                    dest_item.unlink()
                            shutil.move(str(item), str(dest_item))
                        logger.info(f"[RESTIC] Merged contents from {nested_path} to {actual_dest}")

                        # Remove the now-empty nested directory
                        try:
                            nested_path.rmdir()
                        except OSError:
                            pass  # Not empty, that's fine

                    # Try to clean up the empty drive letter directory (e.g., C:\C)
                    drive_letter_dir = Path(f"{windows_drive_letter}:\\{windows_drive_letter}")
                    try:
                        if drive_letter_dir.exists() and not any(drive_letter_dir.iterdir()):
                            drive_letter_dir.rmdir()
                            logger.info(f"[RESTIC] Cleaned up empty directory: {drive_letter_dir}")
                    except OSError:
                        pass  # Directory not empty or other issue

                else:
                    logger.warning(f"[RESTIC] Expected nested path {nested_path} does not exist")

            except Exception as e:
                logger.error(f"[RESTIC] Failed to fix Windows path: {e}")

        # Verify restore by checking if destination exists and has contents
        dest_path = Path(original_dest)
        if dest_path.exists():
            try:
                contents = list(dest_path.iterdir())
                logger.info(f"[RESTIC] Destination {original_dest} exists with {len(contents)} items")
                for item in contents[:10]:  # Log first 10 items
                    logger.debug(f"[RESTIC]   - {item.name}")
            except Exception as e:
                logger.warning(f"[RESTIC] Could not list destination contents: {e}")
        else:
            logger.warning(f"[RESTIC] Destination {original_dest} does not exist after restore!")

        return {
            'success': process.returncode == 0,
            'output': output,
            'bytes': 0,
            'files': 0,
            'error': None if process.returncode == 0 else f"Exit code: {process.returncode}",
        }

    def _init_kopia_repo(self, kopia: Path, dest: str, env: dict) -> bool:
        """Initialize kopia repository if it doesn't exist."""
        # Check if repo exists by running snapshot list command
        check_cmd = [str(kopia), 'repository', 'status', '--json']
        logger.info("[KOPIA] Checking if repository is connected...")

        result = subprocess.run(
            check_cmd,
            capture_output=True,
            text=True,
            env=env,
            creationflags=get_subprocess_flags(),
        )

        if result.returncode == 0:
            logger.info("[KOPIA] Repository already connected")
            return True

        # Try to connect to existing repository
        logger.info("[KOPIA] Attempting to connect to existing repository...")
        connect_cmd = [str(kopia), 'repository', 'connect', 'filesystem', '--path', dest]

        result = subprocess.run(
            connect_cmd,
            capture_output=True,
            text=True,
            env=env,
            creationflags=get_subprocess_flags(),
        )

        if result.returncode == 0:
            logger.info("[KOPIA] Connected to existing repository")
            return True

        # Repository doesn't exist, initialize it
        logger.info("[KOPIA] Repository not found, initializing...")
        try:
            self._ensure_repository_parent_directory(dest)
        except OSError as exc:
            logger.error(f"[KOPIA] Failed to create repository parent directory: {exc}")
            return False

        init_cmd = [str(kopia), 'repository', 'create', 'filesystem', '--path', dest]

        result = subprocess.run(
            init_cmd,
            capture_output=True,
            text=True,
            env=env,
            creationflags=get_subprocess_flags(),
        )

        if result.returncode == 0:
            logger.info("[KOPIA] Repository initialized successfully")
            return True
        else:
            logger.error(f"[KOPIA] Failed to initialize repository: {result.stderr}")
            return False

    def _run_kopia_backup(
        self,
        source: str,
        dest: str,
        excludes: list[str],
        dry_run: bool,
        run_id: str,
        backend_options: dict[str, Any] | None = None,
        smb_server: str | None = None,
        smb_share: str | None = None,
        smb_username: str | None = None,
        smb_password: str | None = None,
        smb_domain: str | None = None,
    ) -> dict[str, Any]:
        """Run kopia backup command."""
        logger.info("[KOPIA] Setting up kopia backup")

        # Normalize paths for Windows (convert UNC forward slashes to backslashes)
        source = self._normalize_windows_path(source)
        dest = self._normalize_windows_path(dest)

        logger.debug(f"[KOPIA] Source: {source}")
        logger.debug(f"[KOPIA] Repository: {dest}")

        # On Windows, connect to SMB share with credentials if provided
        if sys.platform == 'win32' and smb_server and smb_share:
            if not self._connect_windows_smb(smb_server, smb_share, smb_username, smb_password, smb_domain):
                return {
                    'success': False,
                    'output': 'Failed to connect to SMB share',
                    'bytes': 0,
                    'files': 0,
                    'error': f'Failed to connect to \\\\{smb_server}\\{smb_share}',
                }

        try:
            kopia = self._get_tool_path('kopia')
            logger.info(f"[KOPIA] Using kopia at: {kopia}")
        except FileNotFoundError as e:
            logger.error(f"[KOPIA] {e}")
            raise

        backend_options = backend_options or {}

        # Set up environment with password
        # Priority: backend_options > env var > default
        env = os.environ.copy()
        if 'password' in backend_options:
            env['KOPIA_PASSWORD'] = backend_options['password']
            logger.info("[KOPIA] Using password from job configuration")
        elif 'kopia_password' in backend_options:
            env['KOPIA_PASSWORD'] = backend_options['kopia_password']
            logger.info("[KOPIA] Using kopia_password from job configuration")
        elif 'restic_password' in backend_options:
            # Allow using restic_password for compatibility
            env['KOPIA_PASSWORD'] = backend_options['restic_password']
            logger.info("[KOPIA] Using restic_password from job configuration")
        elif 'KOPIA_PASSWORD' not in env:
            env['KOPIA_PASSWORD'] = 'backer-default-password'
            logger.info("[KOPIA] Using default repository password")

        # Initialize/connect to repository if needed
        if not self._init_kopia_repo(kopia, dest, env):
            return {
                'success': False,
                'output': 'Failed to initialize kopia repository',
                'bytes': 0,
                'files': 0,
                'error': 'Repository initialization failed',
            }

        cmd = [str(kopia), 'snapshot', 'create', source, '--json']

        for exclude in excludes:
            cmd.extend(['--ignore-rules', exclude])

        if dry_run:
            cmd.append('--dry-run')

        logger.info(f"[KOPIA] Executing command: {' '.join(cmd)}")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            creationflags=get_subprocess_flags(),
        )

        output_lines = []
        snapshot_id = None
        total_size = 0
        total_files = 0

        for line in process.stdout:
            output_lines.append(line)
            logger.debug(line.strip())

            # Parse JSON output for snapshot info
            try:
                data = json.loads(line)
                # Kopia has different output format
                if 'id' in data:
                    snapshot_id = data.get('id')
                elif 'snapshotID' in data:
                    snapshot_id = data.get('snapshotID')

                # Try to extract stats from rootEntry
                if 'rootEntry' in data:
                    root = data.get('rootEntry', {})
                    summ = root.get('summ', {})
                    total_size = summ.get('size', 0)
                    total_files = summ.get('files', 0)

            except (json.JSONDecodeError, TypeError):
                pass  # Not all lines are JSON

        try:
            process.wait(timeout=300)  # 5 minute timeout for process cleanup
        except subprocess.TimeoutExpired:
            logger.warning("[KOPIA] Process wait timed out, killing process")
            process.kill()
            process.wait(timeout=10)
        output = ''.join(output_lines)

        logger.info(f"[KOPIA] Process completed with return code: {process.returncode}")
        if process.returncode != 0:
            logger.error(f"[KOPIA] Process failed! Output:\n{output}")
        else:
            logger.info("[KOPIA] Backup completed successfully")
            if snapshot_id:
                logger.info(f"[KOPIA] Snapshot ID: {snapshot_id}")

        # Disconnect after backup
        self._disconnect_kopia_repo(kopia, env)

        return {
            'success': process.returncode == 0,
            'output': output,
            'bytes': total_size,
            'files': total_files,
            'snapshot_id': snapshot_id,
            'error': None if process.returncode == 0 else f"Exit code: {process.returncode}",
        }

    def _disconnect_kopia_repo(self, kopia: Path, env: dict) -> None:
        """Disconnect from kopia repository."""
        try:
            subprocess.run(
                [str(kopia), 'repository', 'disconnect'],
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
                creationflags=get_subprocess_flags(),
            )
        except Exception as e:
            logger.debug(f"[KOPIA] Failed to disconnect repository (non-fatal): {e}")

    def _run_kopia_restore(
        self,
        repo: str,
        dest: str,
        snapshot: str | None,
        dry_run: bool,
        run_id: str,
        backend_options: dict[str, Any] | None = None,
        include_path: str | None = None,
    ) -> dict[str, Any]:
        """Run kopia restore command."""
        logger.info("[KOPIA] Setting up kopia restore")

        # Normalize paths for Windows (convert UNC forward slashes to backslashes)
        repo = self._normalize_windows_path(repo)
        dest = self._normalize_windows_path(dest)

        logger.debug(f"[KOPIA] Repository: {repo}")
        logger.debug(f"[KOPIA] Destination: {dest}")
        logger.debug(f"[KOPIA] Snapshot: {snapshot or 'latest'}")
        logger.debug(f"[KOPIA] Include path: {include_path or 'all'}")

        try:
            kopia = self._get_tool_path('kopia')
            logger.info(f"[KOPIA] Using kopia at: {kopia}")
        except FileNotFoundError as e:
            logger.error(f"[KOPIA] {e}")
            raise

        backend_options = backend_options or {}

        # Set up environment with password
        env = os.environ.copy()
        if 'password' in backend_options:
            env['KOPIA_PASSWORD'] = backend_options['password']
            logger.info("[KOPIA] Using password from job configuration")
        elif 'kopia_password' in backend_options:
            env['KOPIA_PASSWORD'] = backend_options['kopia_password']
            logger.info("[KOPIA] Using kopia_password from job configuration")
        elif 'restic_password' in backend_options:
            env['KOPIA_PASSWORD'] = backend_options['restic_password']
            logger.info("[KOPIA] Using restic_password from job configuration")
        elif 'KOPIA_PASSWORD' not in env:
            env['KOPIA_PASSWORD'] = 'backer-default-password'
            logger.info("[KOPIA] Using default repository password")

        # Connect to repository
        connect_cmd = [str(kopia), 'repository', 'connect', 'filesystem', '--path', repo]
        result = subprocess.run(
            connect_cmd,
            capture_output=True,
            text=True,
            env=env,
            creationflags=get_subprocess_flags(),
        )

        if result.returncode != 0:
            return {
                'success': False,
                'output': result.stderr,
                'bytes': 0,
                'files': 0,
                'error': f"Failed to connect to repository: {result.stderr}",
            }

        # Use provided snapshot or get the latest one
        snapshot_id = snapshot
        if not snapshot_id:
            # Query for the latest snapshot
            logger.info("[KOPIA] No snapshot specified, querying for latest...")
            list_cmd = [str(kopia), 'snapshot', 'list', '--json']
            list_result = subprocess.run(
                list_cmd,
                capture_output=True,
                text=True,
                env=env,
                creationflags=get_subprocess_flags(),
            )
            if list_result.returncode == 0:
                try:
                    import json as json_module
                    snapshots = json_module.loads(list_result.stdout)
                    if snapshots:
                        # Get the most recent snapshot (last in list)
                        latest = snapshots[-1]
                        snapshot_id = latest.get('id') or latest.get('rootID')
                        logger.info(f"[KOPIA] Found latest snapshot: {snapshot_id}")
                except (json_module.JSONDecodeError, KeyError, IndexError) as e:
                    logger.warning(f"[KOPIA] Failed to parse snapshot list: {e}")

        if not snapshot_id:
            return {
                'success': False,
                'output': 'No snapshots found in repository',
                'bytes': 0,
                'files': 0,
                'error': 'No snapshots found in repository. Run a backup first.',
            }

        # Build restore command
        # kopia snapshot restore <snapshot-id> <destination>
        cmd = [str(kopia), 'snapshot', 'restore', snapshot_id, dest]

        # Note: Kopia doesn't have a direct --dry-run for restore

        logger.info(f"[KOPIA] Executing command: {' '.join(cmd)}")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            creationflags=get_subprocess_flags(),
        )

        output_lines = []
        for line in process.stdout:
            output_lines.append(line)
            logger.debug(line.strip())

        try:
            process.wait(timeout=300)  # 5 minute timeout for process cleanup
        except subprocess.TimeoutExpired:
            logger.warning("[KOPIA] Restore process wait timed out, killing process")
            process.kill()
            process.wait(timeout=10)
        output = ''.join(output_lines)

        logger.info(f"[KOPIA] Process completed with return code: {process.returncode}")
        if process.returncode != 0:
            logger.error(f"[KOPIA] Process failed! Output:\n{output}")
        else:
            logger.info("[KOPIA] Restore completed successfully")

        # Disconnect after restore
        self._disconnect_kopia_repo(kopia, env)

        # Verify restore
        dest_path = Path(dest)
        if dest_path.exists():
            try:
                contents = list(dest_path.iterdir())
                logger.info(f"[KOPIA] Destination {dest} exists with {len(contents)} items")
            except Exception as e:
                logger.warning(f"[KOPIA] Could not list destination contents: {e}")
        else:
            logger.warning(f"[KOPIA] Destination {dest} does not exist after restore!")

        return {
            'success': process.returncode == 0,
            'output': output,
            'bytes': 0,
            'files': 0,
            'error': None if process.returncode == 0 else f"Exit code: {process.returncode}",
        }

    def _run_proxy_backup(
        self,
        source: str,
        dest: str,
        excludes: list[str],
        dry_run: bool,
        run_id: str,
        backend_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run proxy backup - streams backup data to server via HTTP.

        Used for LOCAL repository type where the server stores backups
        in a local directory.
        """
        logger.info("[PROXY] Setting up proxy backup")
        logger.info(f"[PROXY] Source: {source}")
        logger.info(f"[PROXY] Destination (proxy URI): {dest}")

        try:
            # Import proxy backend dependencies
            from backer.backends.base import BackupDestination, BackupSource
            from backer.backends.proxy import ProxyBackend

            # Build backend config with proxy URI and agent credentials
            config = {
                'location': dest,
                'client_id': self.client_id,
                'client_secret': self.client_secret,
            }

            # Add password from backend_options if provided
            backend_options = backend_options or {}
            if 'password' in backend_options:
                config['password'] = backend_options['password']

            # Create proxy backend instance
            proxy_backend = ProxyBackend(config)

            # Create source and destination objects
            backup_source = BackupSource(path=source, excludes=excludes)
            backup_dest = BackupDestination(path=dest)

            # Progress callback for logging
            def progress_callback(bytes_done: int = 0, files_done: int = 0, current_file: str = ''):
                if files_done > 0 and files_done % 100 == 0:
                    logger.info(f"[PROXY] Progress: {files_done} files processed")
                    self._report_progress(
                        run_id,
                        'running',
                        min(50, files_done // 10),  # Estimate progress
                        f"Processed {files_done} files..."
                    )

            # Execute backup via proxy
            logger.info("[PROXY] Starting backup via proxy backend")
            result = proxy_backend.backup(
                source=backup_source,
                destination=backup_dest,
                dry_run=dry_run,
                progress_callback=progress_callback,
            )

            # Convert BackendResult to dict format expected by _execute_backup
            return {
                'success': result.success,
                'output': result.output or '',
                'bytes': result.bytes_transferred,
                'files': result.files_transferred,
                'snapshot_id': result.metadata.get('snapshot_id') if result.metadata else None,
                'error': result.errors[0] if result.errors else None,
            }

        except ImportError as e:
            logger.error(f"[PROXY] Failed to import proxy backend: {e}")
            return {
                'success': False,
                'output': '',
                'bytes': 0,
                'files': 0,
                'error': f"Proxy backend not available: {e}",
            }
        except Exception as e:
            logger.error(f"[PROXY] Backup failed: {e}", exc_info=True)
            return {
                'success': False,
                'output': '',
                'bytes': 0,
                'files': 0,
                'error': str(e),
            }

    def _run_proxy_restore(
        self,
        source: str,
        dest: str,
        snapshot: str | None,
        dry_run: bool,
        run_id: str,
        backend_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run proxy restore - streams restore data from server via HTTP.

        Used for LOCAL repository type where the server streams backup
        data back to the agent.
        """
        logger.info("[PROXY] Setting up proxy restore")
        logger.info(f"[PROXY] Source (proxy URI): {source}")
        logger.info(f"[PROXY] Destination: {dest}")
        logger.info(f"[PROXY] Snapshot: {snapshot or 'latest'}")

        try:
            # Import proxy backend dependencies
            from backer.backends.base import BackupDestination
            from backer.backends.proxy import ProxyBackend

            # Build backend config with proxy URI and agent credentials
            config = {
                'location': source,
                'client_id': self.client_id,
                'client_secret': self.client_secret,
            }

            # Add password from backend_options if provided
            backend_options = backend_options or {}
            if 'password' in backend_options:
                config['password'] = backend_options['password']

            # Create proxy backend instance
            proxy_backend = ProxyBackend(config)

            # Create source (which is actually the repo location for restore)
            backup_source = BackupDestination(path=source)

            # Progress callback for logging
            def progress_callback(bytes_done: int = 0, files_done: int = 0, current_file: str = ''):
                if bytes_done > 0 and bytes_done % (10 * 1024 * 1024) == 0:  # Every 10MB
                    logger.info(f"[PROXY] Progress: {bytes_done / 1024 / 1024:.1f}MB received")
                    self._report_progress(
                        run_id,
                        'running',
                        min(50, int(bytes_done / 1024 / 1024)),
                        f"Received {bytes_done / 1024 / 1024:.1f}MB..."
                    )

            # Execute restore via proxy
            logger.info("[PROXY] Starting restore via proxy backend")
            result = proxy_backend.restore(
                source=backup_source,
                destination=Path(dest),
                snapshot=snapshot,
                dry_run=dry_run,
                progress_callback=progress_callback,
            )

            # Convert BackendResult to dict format expected by _execute_restore
            return {
                'success': result.success,
                'output': result.output or '',
                'bytes': result.bytes_transferred,
                'files': result.files_transferred,
                'error': result.errors[0] if result.errors else None,
            }

        except ImportError as e:
            logger.error(f"[PROXY] Failed to import proxy backend: {e}")
            return {
                'success': False,
                'output': '',
                'bytes': 0,
                'files': 0,
                'error': f"Proxy backend not available: {e}",
            }
        except Exception as e:
            logger.error(f"[PROXY] Restore failed: {e}", exc_info=True)
            return {
                'success': False,
                'output': '',
                'bytes': 0,
                'files': 0,
                'error': str(e),
            }

    def _execute_browse_filesystem(self, payload: dict[str, Any]):
        """Execute filesystem browse command and report results."""
        request_id = payload.get('request_id')
        if not request_id:
            logger.error("[BROWSE] Missing request_id in payload")
            return

        path = payload.get('path', '')

        logger.info(f"[BROWSE] Browsing filesystem at: {path or '(root)'}")

        try:
            entries = []

            if not path:
                # Return root directories based on platform
                if sys.platform == 'win32':
                    # List available drives on Windows
                    import ctypes
                    drives = []
                    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
                    for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
                        if bitmask & 1:
                            drives.append(f"{letter}:\\")
                        bitmask >>= 1

                    for drive in drives:
                        entries.append({
                            'name': drive,
                            'path': drive,
                            'is_dir': True,
                            'size': 0,
                        })
                else:
                    # On Unix, start from /home or /
                    home = Path.home()
                    if home.exists():
                        entries.append({
                            'name': 'Home',
                            'path': str(home),
                            'is_dir': True,
                            'size': 0,
                        })
                    entries.append({
                        'name': '/',
                        'path': '/',
                        'is_dir': True,
                        'size': 0,
                    })

                actual_path = ''
            else:
                # List contents of the specified path
                browse_path = Path(path)
                actual_path = str(browse_path)

                if not browse_path.exists():
                    raise FileNotFoundError(f"Path does not exist: {path}")

                if not browse_path.is_dir():
                    raise NotADirectoryError(f"Path is not a directory: {path}")

                # Windows system folders to skip (they can hang or cause errors)
                skip_names = {
                    '$recycle.bin', 'system volume information', '$windows.~bt',
                    '$windows.~ws', 'recovery', 'config.msi', 'msocache',
                    '$syswol', 'found.000', 'found.001',
                }

                # Use os.scandir for better performance (caches stat info)
                dirs = []
                files = []

                try:
                    with os.scandir(str(browse_path)) as scanner:
                        for entry in scanner:
                            try:
                                # Skip hidden system folders on Windows
                                name_lower = entry.name.lower()
                                if name_lower in skip_names:
                                    continue

                                # Use cached is_dir from scandir (much faster)
                                is_dir = entry.is_dir(follow_symlinks=False)

                                item_entry = {
                                    'name': entry.name,
                                    'path': entry.path,
                                    'is_dir': is_dir,
                                    'size': 0,
                                }

                                if is_dir:
                                    dirs.append(item_entry)
                                else:
                                    # Get file size from cached stat
                                    try:
                                        item_entry['size'] = entry.stat().st_size
                                    except (OSError, PermissionError):
                                        pass
                                    files.append(item_entry)

                                # Stop early if we have enough entries
                                if len(dirs) + len(files) >= 500:
                                    break

                            except (OSError, PermissionError):
                                # Skip items we can't access
                                continue
                except PermissionError:
                    raise PermissionError(f"Permission denied: {path}")

                # Sort directories and files by name, then combine (dirs first)
                dirs.sort(key=lambda x: x['name'].lower())
                files.sort(key=lambda x: x['name'].lower())
                entries = (dirs + files)[:200]

            # Report results to server
            self._make_request(
                f'/api/v1/browse/{request_id}/results',
                method='POST',
                data={
                    'success': True,
                    'path': actual_path,
                    'entries': entries,
                },
            )

            logger.info(f"[BROWSE] Sent {len(entries)} entries for path: {path or '(root)'}")

        except Exception as e:
            logger.error(f"[BROWSE] Failed to browse {path}: {e}")
            # Report error to server
            try:
                self._make_request(
                    f'/api/v1/browse/{request_id}/results',
                    method='POST',
                    data={
                        'success': False,
                        'path': path,
                        'entries': [],
                        'error': str(e),
                    },
                )
            except Exception as report_err:
                logger.error(f"[BROWSE] Failed to report error: {report_err}")

    def _report_progress(
        self,
        run_id: str,
        status: str,
        percent: int,
        message: str,
    ):
        """Report progress to server."""
        try:
            self._make_request(
                '/api/v1/progress',
                method='POST',
                data={
                    'run_id': run_id,
                    'status': status,
                    'progress_percent': percent,
                    'message': message,
                },
            )
        except Exception as e:
            logger.warning(f"Failed to report progress: {e}")

    def _report_result(
        self,
        run_id: str,
        job_name: str,
        success: bool,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        bytes_transferred: int = 0,
        files_transferred: int = 0,
        output: str = '',
        error: str | None = None,
        snapshot_id: str | None = None,
    ):
        """Report backup result to server."""
        try:
            self._make_request(
                '/api/v1/results',
                method='POST',
                data={
                    'run_id': run_id,
                    'job_name': job_name,
                    'client_id': self.client_id,
                    'success': success,
                    'started_at': started_at.isoformat() if started_at else None,
                    'finished_at': finished_at.isoformat() if finished_at else None,
                    'bytes_transferred': bytes_transferred,
                    'files_transferred': files_transferred,
                    'output': output[:5000],  # Limit output size
                    'errors': [error] if error else [],
                    'snapshot_id': snapshot_id,  # For restic backups
                },
            )
        except Exception as e:
            logger.error(f"Failed to report result: {e}")
