"""
Backer Agent Service

Background service that polls the server for commands and executes backups.
"""

import base64
import hashlib
import json
import logging
import subprocess
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

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
            import os
            import sys
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

        self._running = False
        self._thread: threading.Thread | None = None

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

        logger.info(f"Processing command {cmd_id}: {cmd_type}")

        try:
            if cmd_type == 'backup':
                self._execute_backup(payload)
            elif cmd_type == 'restore':
                self._execute_restore(payload)
            else:
                logger.warning(f"Unknown command type: {cmd_type}")

            # Acknowledge command
            self._make_request(f'/api/v1/commands/{cmd_id}/ack', method='POST')

        except Exception as e:
            logger.error(f"Command {cmd_id} failed: {e}")
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
        dry_run = payload.get('dry_run', False)

        self._update_status(f"Backing up: {job_name}")
        logger.info(f"Starting backup: {source_path} -> {destination_path}")

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
                    source_path, destination_path, excludes, dry_run, run_id
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
        dry_run = payload.get('dry_run', False)

        self._update_status(f"Restoring: {job_name}")
        logger.info(f"Starting restore: {source_path} -> {destination_path}")

        started_at = datetime.now()
        self._report_progress(run_id, 'running', 0, 'Starting restore...')

        try:
            if backend == 'rclone':
                # For restore, swap source and dest
                result = self._run_rclone_sync(
                    source_path, destination_path, [], dry_run, run_id
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

    def _run_rclone_sync(
        self,
        source: str,
        dest: str,
        excludes: list[str],
        dry_run: bool,
        run_id: str,
    ) -> dict[str, Any]:
        """Run rclone sync command."""
        import sys

        if sys.platform == 'win32':
            rclone = self.tools_dir / 'rclone.exe'
        else:
            rclone = self.tools_dir / 'rclone'

        if not rclone.exists():
            raise FileNotFoundError(f"rclone not found at {rclone}")

        cmd = [str(rclone), 'sync', source, dest, '--progress', '-v']

        for exclude in excludes:
            cmd.extend(['--exclude', exclude])

        if dry_run:
            cmd.append('--dry-run')

        logger.info(f"Running: {' '.join(cmd)}")

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

        return {
            'success': process.returncode == 0,
            'output': output,
            'bytes': bytes_transferred,
            'files': files_transferred,
            'error': None if process.returncode == 0 else f"Exit code: {process.returncode}",
        }

    def _run_restic_backup(
        self,
        source: str,
        dest: str,
        excludes: list[str],
        dry_run: bool,
        run_id: str,
    ) -> dict[str, Any]:
        """Run restic backup command."""
        import sys

        if sys.platform == 'win32':
            restic = self.tools_dir / 'restic.exe'
        else:
            restic = self.tools_dir / 'restic'

        if not restic.exists():
            raise FileNotFoundError(f"restic not found at {restic}")

        cmd = [str(restic), '-r', dest, 'backup', source, '-v']

        for exclude in excludes:
            cmd.extend(['--exclude', exclude])

        if dry_run:
            cmd.append('--dry-run')

        logger.info(f"Running: {' '.join(cmd)}")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        output_lines = []
        for line in process.stdout:
            output_lines.append(line)
            logger.debug(line.strip())

        process.wait()
        output = ''.join(output_lines)

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
                },
            )
        except Exception as e:
            logger.error(f"Failed to report result: {e}")
