"""
Backer Agent Service

Background service that polls the server for commands and executes backups.
"""

import base64
import json
import logging
import os
import platform
import socket
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

from backer.core.repo_metadata import RepositoryMetadata


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
        logger.info(f"Tools directory: {self.tools_dir}")

        # Initialize tool manager for automatic tool downloads
        self._tool_manager = None
        self._tools_ready = False

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
        req.add_header('User-Agent', 'Backer-Agent/1.0')

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

    def _process_command(self, command: dict[str, Any]):
        """Process a command from the server."""
        cmd_id = command.get('id')
        cmd_type = command.get('command_type')
        payload = command.get('payload', {})

        logger.info(f"[COMMAND] Processing command {cmd_id}: {cmd_type}")
        logger.debug(f"[COMMAND] Full payload: {json.dumps(payload, indent=2)}")

        try:
            if cmd_type == 'backup':
                self._execute_backup(payload)
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

    def _execute_backup(self, payload: dict[str, Any]):
        """Execute a backup command."""
        run_id = payload.get('run_id')
        job_name = payload.get('job_name')
        source_path = payload.get('source_path')
        destination_path = payload.get('destination_path')
        backend = payload.get('backend', 'rclone')
        excludes = payload.get('excludes', [])
        backend_options = payload.get('backend_options', {})
        dry_run = payload.get('dry_run', False)

        # SMB credentials for metadata writing
        smb_server = payload.get('smb_server')
        smb_share = payload.get('smb_share')
        smb_username = payload.get('smb_username')
        smb_password = payload.get('smb_password')
        smb_domain = payload.get('smb_domain')

        self._update_status(f"Backing up: {job_name}")
        logger.info(f"[BACKUP] Starting backup job '{job_name}' (run_id: {run_id})")
        logger.info(f"[BACKUP] Source: {source_path}")
        logger.info(f"[BACKUP] Destination: {destination_path}")
        logger.info(f"[BACKUP] Backend: {backend}")
        logger.info(f"[BACKUP] Tools directory: {self.tools_dir}")
        logger.info(f"[BACKUP] Excludes: {excludes}")
        logger.info(f"[BACKUP] Dry run: {dry_run}")
        if smb_server:
            logger.info(f"[BACKUP] SMB: //{smb_server}/{smb_share}")

        started_at = datetime.now()

        # Report progress: starting
        self._report_progress(run_id, 'running', 0, 'Starting backup...')

        try:
            if backend == 'rclone':
                result = self._run_rclone_sync(
                    source_path, destination_path, excludes, dry_run, run_id
                )
            elif backend == 'restic':
                result = self._run_restic_backup(
                    source_path, destination_path, excludes, dry_run, run_id,
                    backend_options=backend_options,
                )
            elif backend == 'kopia':
                result = self._run_kopia_backup(
                    source_path, destination_path, excludes, dry_run, run_id,
                    backend_options=backend_options,
                )
            else:
                raise ValueError(f"Unknown backend: {backend}")

            finished_at = datetime.now()

            # Report result
            self._report_result(
                run_id=run_id,
                job_name=job_name,
                success=result['success'],
                started_at=started_at,
                finished_at=finished_at,
                bytes_transferred=result.get('bytes', 0),
                files_transferred=result.get('files', 0),
                output=result.get('output', ''),
                error=result.get('error'),
                snapshot_id=result.get('snapshot_id'),  # For restic backups
            )

            if result['success']:
                self._update_status(f"Backup complete: {job_name}")
                # Write metadata to repository for discovery
                self._write_repo_metadata(
                    repo_path=destination_path,
                    job_name=job_name,
                    run_id=run_id,
                    source_path=source_path,
                    backend=backend,
                    result=result,
                    started_at=started_at,
                    finished_at=finished_at,
                    smb_server=smb_server,
                    smb_share=smb_share,
                    smb_username=smb_username,
                    smb_password=smb_password,
                    smb_domain=smb_domain,
                )
            else:
                self._update_status(f"Backup failed: {job_name}")

        except Exception as e:
            self._report_result(
                run_id=run_id,
                job_name=job_name,
                success=False,
                started_at=started_at,
                finished_at=datetime.now(),
                error=str(e),
            )
            self._update_status(f"Backup error: {e}")
            raise

    def _execute_restore(self, payload: dict[str, Any]):
        """Execute a restore command."""
        run_id = payload.get('run_id')
        job_name = payload.get('job_name')
        source_path = payload.get('source_path')
        destination_path = payload.get('destination_path')
        backend = payload.get('backend', 'rclone')
        snapshot = payload.get('snapshot')  # For restic: snapshot ID or "latest"
        source_subfolder = payload.get('source_subfolder', '')  # For restic: --include path
        clean_restore = payload.get('clean_restore', False)
        backend_options = payload.get('backend_options', {})
        dry_run = payload.get('dry_run', False)

        self._update_status(f"Restoring: {job_name}")
        logger.info(f"Starting restore: {source_path} -> {destination_path}")
        logger.info(f"Clean restore: {clean_restore}, Dry run: {dry_run}")

        started_at = datetime.now()
        self._report_progress(run_id, 'running', 0, 'Starting restore...')

        try:
            # For clean restore with restic/kopia, clear destination first
            # (rclone sync already handles this automatically)
            if clean_restore and backend in ('restic', 'kopia') and not dry_run:
                dest_path = Path(destination_path)
                if dest_path.exists():
                    logger.info(f"[RESTORE] Clean restore: clearing destination {destination_path}")
                    import shutil
                    shutil.rmtree(destination_path)
                    # Don't recreate the directory - let restic create it
                    # This avoids metadata conflicts that can cause restore issues

            if backend == 'rclone':
                # rclone sync deletes files at dest that aren't in source
                result = self._run_rclone_sync(
                    source_path, destination_path, [], dry_run, run_id
                )
            elif backend == 'restic':
                result = self._run_restic_restore(
                    source_path, destination_path, snapshot, dry_run, run_id,
                    backend_options=backend_options,
                    include_path=source_subfolder,
                )
            elif backend == 'kopia':
                result = self._run_kopia_restore(
                    source_path, destination_path, snapshot, dry_run, run_id,
                    backend_options=backend_options,
                    include_path=source_subfolder,
                )
            else:
                raise ValueError(f"Restore not supported for backend: {backend}")

            finished_at = datetime.now()

            self._report_result(
                run_id=run_id,
                job_name=f"restore:{job_name}",
                success=result['success'],
                started_at=started_at,
                finished_at=finished_at,
                bytes_transferred=result.get('bytes', 0),
                files_transferred=result.get('files', 0),
                output=result.get('output', ''),
                error=result.get('error'),
            )

            if result['success']:
                self._update_status(f"Restore complete: {job_name}")
            else:
                self._update_status(f"Restore failed: {job_name}")

        except Exception as e:
            self._report_result(
                run_id=run_id,
                job_name=f"restore:{job_name}",
                success=False,
                started_at=started_at,
                finished_at=datetime.now(),
                error=str(e),
            )
            raise

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

        raise FileNotFoundError(f"{tool_name} not found. Run ensure_tools_installed() first.")

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
    ) -> dict[str, Any]:
        """Run restic backup command."""
        logger.info("[RESTIC] Setting up restic backup")

        # Normalize paths for Windows (convert UNC forward slashes to backslashes)
        source = self._normalize_windows_path(source)
        dest = self._normalize_windows_path(dest)

        logger.debug(f"[RESTIC] Source: {source}")
        logger.debug(f"[RESTIC] Repository: {dest}")

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
    ) -> dict[str, Any]:
        """Run kopia backup command."""
        logger.info("[KOPIA] Setting up kopia backup")

        # Normalize paths for Windows (convert UNC forward slashes to backslashes)
        source = self._normalize_windows_path(source)
        dest = self._normalize_windows_path(dest)

        logger.debug(f"[KOPIA] Source: {source}")
        logger.debug(f"[KOPIA] Repository: {dest}")

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
        if 'kopia_password' in backend_options:
            env['KOPIA_PASSWORD'] = backend_options['kopia_password']
            logger.info("[KOPIA] Using password from job configuration")
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
        if 'kopia_password' in backend_options:
            env['KOPIA_PASSWORD'] = backend_options['kopia_password']
            logger.info("[KOPIA] Using password from job configuration")
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

    def _write_repo_metadata(
        self,
        repo_path: str,
        job_name: str,
        run_id: str,
        source_path: str,
        backend: str,
        result: dict[str, Any],
        started_at: datetime,
        finished_at: datetime,
        smb_server: str | None = None,
        smb_share: str | None = None,
        smb_username: str | None = None,
        smb_password: str | None = None,
        smb_domain: str | None = None,
    ) -> None:
        """Write backup metadata to the repository for discovery.

        This enables a new Backer server to discover existing backups
        when pointed to a repository.

        For SMB shares on Linux, uses smbclient to write files directly.
        For local paths or Windows, uses direct filesystem access.
        """
        try:
            # Check if we need to use SMB for metadata writing (Linux with SMB credentials)
            use_smb = (
                sys.platform != 'win32'
                and smb_server
                and smb_share
            )

            if use_smb:
                self._write_repo_metadata_smb(
                    smb_server=smb_server,
                    smb_share=smb_share,
                    smb_username=smb_username,
                    smb_password=smb_password,
                    smb_domain=smb_domain,
                    repo_path=repo_path,
                    job_name=job_name,
                    run_id=run_id,
                    source_path=source_path,
                    backend=backend,
                    result=result,
                    started_at=started_at,
                    finished_at=finished_at,
                )
            else:
                # Use local filesystem (works on Windows with UNC paths)
                self._write_repo_metadata_local(
                    repo_path=repo_path,
                    job_name=job_name,
                    run_id=run_id,
                    source_path=source_path,
                    backend=backend,
                    result=result,
                    started_at=started_at,
                    finished_at=finished_at,
                )

        except Exception as e:
            # Don't fail the backup just because metadata writing failed
            logger.warning(f"[METADATA] Failed to write repository metadata: {e}")

    def _write_repo_metadata_local(
        self,
        repo_path: str,
        job_name: str,
        run_id: str,
        source_path: str,
        backend: str,
        result: dict[str, Any],
        started_at: datetime,
        finished_at: datetime,
    ) -> None:
        """Write metadata using local filesystem access."""
        repo_path = self._normalize_windows_path(repo_path)
        logger.info(f"[METADATA-LOCAL] Writing metadata to: {repo_path}")

        try:
            repo = RepositoryMetadata(repo_path)

            # Initialize repository metadata if needed
            if not repo.is_initialized():
                logger.info(f"[METADATA-LOCAL] Initializing metadata directory at {repo.metadata_dir}")
                repo.initialize()
            else:
                logger.info(f"[METADATA-LOCAL] Metadata already initialized at {repo.metadata_dir}")

            # Save agent information
            repo.save_agent(
                agent_id=self.client_id,
                agent_data={
                    "hostname": socket.gethostname(),
                    "platform": sys.platform,
                    "os_info": f"{platform.system()} {platform.release()}",
                    "python_version": platform.python_version(),
                },
            )
            logger.debug(f"[METADATA-LOCAL] Saved agent info for {self.client_id}")

            # Save job configuration
            repo.save_job(
                job_name=job_name,
                job_config={
                    "source_path": source_path,
                    "backend": backend,
                    "client_id": self.client_id,
                },
            )

            # Save run record
            run_data = {
                "status": "success" if result.get("success") else "failed",
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "bytes_transferred": result.get("bytes", 0),
                "files_transferred": result.get("files", 0),
                "snapshot_id": result.get("snapshot_id"),
                "agent_id": self.client_id,
                "hostname": socket.gethostname(),
            }
            repo.save_job_run(job_name, run_id, run_data)

            # For restic/kopia, also save snapshot metadata
            snapshot_id = result.get("snapshot_id")
            if snapshot_id and backend in ("restic", "kopia"):
                repo.save_snapshot(
                    snapshot_id=snapshot_id,
                    snapshot_data={
                        "job_name": job_name,
                        "run_id": run_id,
                        "hostname": socket.gethostname(),
                        "paths": [source_path],
                        "time": finished_at.isoformat(),
                        "backend": backend,
                    },
                )

            logger.info(f"[METADATA-LOCAL] Successfully wrote metadata to: {repo_path}")

        except Exception as e:
            logger.error(f"[METADATA-LOCAL] Failed to write metadata to {repo_path}: {e}")

    def _write_repo_metadata_smb(
        self,
        smb_server: str,
        smb_share: str,
        smb_username: str | None,
        smb_password: str | None,
        smb_domain: str | None,
        repo_path: str,
        job_name: str,
        run_id: str,
        source_path: str,
        backend: str,
        result: dict[str, Any],
        started_at: datetime,
        finished_at: datetime,
    ) -> None:
        """Write metadata to SMB share using smbclient (for Linux agents)."""
        import json
        import tempfile

        # Extract subpath from repo_path (e.g., //server/share/subpath -> subpath)
        subpath = ""
        if repo_path:
            # Remove //server/share prefix to get subpath
            path_parts = repo_path.replace("\\", "/").lstrip("/").split("/")
            if len(path_parts) > 2:
                subpath = "/".join(path_parts[2:])

        # Build base path for metadata
        metadata_base = f"{subpath}/.backer" if subpath else ".backer"

        logger.info(f"[METADATA-SMB] Writing metadata to //{smb_server}/{smb_share}/{metadata_base}")

        def smb_write_file(remote_path: str, content: str) -> bool:
            """Write content to a file on SMB share using smbclient."""
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
                f.write(content)
                local_path = f.name

            try:
                # Build smbclient command
                cmd = ["smbclient", f"//{smb_server}/{smb_share}", "-t", "10"]

                # Add authentication
                if smb_username and smb_password:
                    with tempfile.NamedTemporaryFile(mode='w', delete=False) as auth_file:
                        auth_file.write(f"username={smb_username}\n")
                        auth_file.write(f"password={smb_password}\n")
                        if smb_domain:
                            auth_file.write(f"domain={smb_domain}\n")
                        auth_path = auth_file.name
                    cmd.extend(["-A", auth_path])
                else:
                    cmd.append("-N")  # No password

                # Ensure parent directory exists and upload file
                parent_dir = "/".join(remote_path.split("/")[:-1])
                filename = remote_path.split("/")[-1]

                # Create directories and upload
                smb_commands = []
                # Create each directory level
                dirs_to_create = []
                current = ""
                for part in parent_dir.split("/"):
                    if part:
                        current = f"{current}/{part}" if current else part
                        dirs_to_create.append(current)
                for d in dirs_to_create:
                    smb_commands.append(f"mkdir {d}")
                smb_commands.append(f"cd {parent_dir}")
                smb_commands.append(f"put {local_path} {filename}")

                cmd.extend(["-c", "; ".join(smb_commands)])

                logger.debug(f"[METADATA-SMB] Running: smbclient //{smb_server}/{smb_share} -c '{'; '.join(smb_commands)}'")
                proc_result = subprocess.run(cmd, capture_output=True, timeout=30)

                # Clean up auth file if created
                if smb_username and smb_password:
                    try:
                        os.unlink(auth_path)
                    except Exception:
                        pass

                stdout = proc_result.stdout.decode('utf-8', errors='replace')
                stderr = proc_result.stderr.decode('utf-8', errors='replace')

                if proc_result.returncode != 0:
                    # mkdir errors are ok (directory might exist)
                    if "NT_STATUS_OBJECT_NAME_COLLISION" in stderr or "NT_STATUS_OBJECT_NAME_COLLISION" in stdout:
                        # Directory already exists, that's fine
                        pass
                    elif "NT_STATUS" in stderr or "NT_STATUS" in stdout:
                        logger.warning(f"[METADATA-SMB] smbclient error for {remote_path}: {stderr or stdout}")
                        return False
                    else:
                        # Other error
                        logger.warning(f"[METADATA-SMB] smbclient failed (rc={proc_result.returncode}) for {remote_path}: {stderr or stdout}")
                        return False

                logger.debug(f"[METADATA-SMB] Successfully wrote {remote_path}")
                return True

            finally:
                try:
                    os.unlink(local_path)
                except Exception:
                    pass

        # Check if smbclient is available
        try:
            subprocess.run(["which", "smbclient"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.error("[METADATA-SMB] smbclient is not installed - cannot write metadata to SMB share")
            logger.error("[METADATA-SMB] Install with: apt install smbclient (Debian/Ubuntu) or yum install samba-client (RHEL)")
            return

        # Write metadata.json
        metadata = {
            "version": "1.0",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "repo_type": "smb",
        }
        if not smb_write_file(f"{metadata_base}/metadata.json", json.dumps(metadata, indent=2)):
            logger.error("[METADATA-SMB] Failed to write metadata.json - aborting")
            return

        # Write agent info
        agent_data = {
            "agent_id": self.client_id,
            "hostname": socket.gethostname(),
            "platform": sys.platform,
            "os_info": f"{platform.system()} {platform.release()}",
            "python_version": platform.python_version(),
            "first_seen": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        if not smb_write_file(
            f"{metadata_base}/agents/{self.client_id}.json",
            json.dumps(agent_data, indent=2)
        ):
            logger.warning("[METADATA-SMB] Failed to write agent info")

        # Write job config
        job_data = {
            "job_name": job_name,
            "config": {
                "source_path": source_path,
                "backend": backend,
                "client_id": self.client_id,
            },
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        safe_job_name = job_name.replace("/", "_").replace("\\", "_")
        smb_write_file(
            f"{metadata_base}/jobs/{safe_job_name}/config.json",
            json.dumps(job_data, indent=2)
        )

        # Write run record
        run_data = {
            "run_id": run_id,
            "job_name": job_name,
            "status": "success" if result.get("success") else "failed",
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "bytes_transferred": result.get("bytes", 0),
            "files_transferred": result.get("files", 0),
            "snapshot_id": result.get("snapshot_id"),
            "agent_id": self.client_id,
            "hostname": socket.gethostname(),
            "recorded_at": datetime.now().isoformat(),
        }
        safe_run_id = run_id.replace("/", "_").replace("\\", "_")
        smb_write_file(
            f"{metadata_base}/jobs/{safe_job_name}/runs/{safe_run_id}.json",
            json.dumps(run_data, indent=2)
        )

        # Write snapshot metadata for restic/kopia
        snapshot_id = result.get("snapshot_id")
        if snapshot_id and backend in ("restic", "kopia"):
            snapshot_data = {
                "snapshot_id": snapshot_id,
                "job_name": job_name,
                "run_id": run_id,
                "hostname": socket.gethostname(),
                "paths": [source_path],
                "time": finished_at.isoformat(),
                "backend": backend,
                "recorded_at": datetime.now().isoformat(),
            }
            short_id = snapshot_id[:12] if len(snapshot_id) > 12 else snapshot_id
            smb_write_file(
                f"{metadata_base}/snapshots/{short_id}.json",
                json.dumps(snapshot_data, indent=2)
            )

        logger.info(f"[METADATA-SMB] Successfully wrote metadata to //{smb_server}/{smb_share}/{metadata_base}")
