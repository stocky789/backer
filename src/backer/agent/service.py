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
