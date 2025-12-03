"""
Backer Agent Service

Background service that polls the server for commands and executes backups.
"""

import base64
import json
import logging
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
        poll_interval: int = 30,
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
        required_tools = ['rclone', 'restic']

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

        with urllib.request.urlopen(req, timeout=30) as response:
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
        """Main service loop."""
        while self._running:
            try:
                self._heartbeat()
            except Exception as e:
                logger.error(f"Heartbeat failed: {e}")
                self._update_status(f"Error: {e}")

            # Sleep in small increments to allow quick shutdown
            for _ in range(self.poll_interval):
                if not self._running:
                    break
                time.sleep(1)

    def _heartbeat(self):
        """Send heartbeat to server and process any commands."""
        self._update_status("Checking for jobs...")

        try:
            response = self._make_request(
                '/api/v1/clients/heartbeat',
                method='POST',
                data={
                    'client_id': self.client_id,
                    'status': 'online',
                },
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

        self._update_status(f"Backing up: {job_name}")
        logger.info(f"[BACKUP] Starting backup job '{job_name}' (run_id: {run_id})")
        logger.info(f"[BACKUP] Source: {source_path}")
        logger.info(f"[BACKUP] Destination: {destination_path}")
        logger.info(f"[BACKUP] Backend: {backend}")
        logger.info(f"[BACKUP] Tools directory: {self.tools_dir}")
        logger.info(f"[BACKUP] Excludes: {excludes}")
        logger.info(f"[BACKUP] Dry run: {dry_run}")

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
        clean_restore = payload.get('clean_restore', False)
        backend_options = payload.get('backend_options', {})
        dry_run = payload.get('dry_run', False)

        self._update_status(f"Restoring: {job_name}")
        logger.info(f"Starting restore: {source_path} -> {destination_path}")
        logger.info(f"Clean restore: {clean_restore}, Dry run: {dry_run}")

        started_at = datetime.now()
        self._report_progress(run_id, 'running', 0, 'Starting restore...')

        try:
            # For clean restore with restic, clear destination first
            # (rclone sync already handles this automatically)
            if clean_restore and backend == 'restic' and not dry_run:
                dest_path = Path(destination_path)
                if dest_path.exists():
                    logger.info(f"[RESTORE] Clean restore: clearing destination {destination_path}")
                    import shutil
                    shutil.rmtree(destination_path)
                    dest_path.mkdir(parents=True, exist_ok=True)

            if backend == 'rclone':
                # rclone sync deletes files at dest that aren't in source
                result = self._run_rclone_sync(
                    source_path, destination_path, [], dry_run, run_id
                )
            elif backend == 'restic':
                result = self._run_restic_restore(
                    source_path, destination_path, snapshot, dry_run, run_id,
                    backend_options=backend_options,
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

        process.wait()
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

        process.wait()
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
    ) -> dict[str, Any]:
        """Run restic restore command."""
        logger.info("[RESTIC] Setting up restic restore")
        logger.debug(f"[RESTIC] Repository: {repo}")
        logger.debug(f"[RESTIC] Destination: {dest}")
        logger.debug(f"[RESTIC] Snapshot: {snapshot or 'latest'}")

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

        # Build restore command
        # restic restore <snapshot> --target <dest> --delete
        # --delete removes files in destination that are not in the snapshot
        cmd = [str(restic), '-r', repo, 'restore', snapshot_id, '--target', dest, '-v']

        if dry_run:
            cmd.append('--dry-run')

        logger.info(f"[RESTIC] Executing command: {' '.join(cmd)}")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )

        output_lines = []
        for line in process.stdout:
            output_lines.append(line)
            logger.debug(line.strip())

        process.wait()
        output = ''.join(output_lines)

        logger.info(f"[RESTIC] Process completed with return code: {process.returncode}")
        if process.returncode != 0:
            logger.error(f"[RESTIC] Process failed! Output:\n{output}")
        else:
            logger.info("[RESTIC] Restore completed successfully")

        return {
            'success': process.returncode == 0,
            'output': output,
            'bytes': 0,
            'files': 0,
            'error': None if process.returncode == 0 else f"Exit code: {process.returncode}",
        }

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
