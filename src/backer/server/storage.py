"""Storage backend for server state (clients, jobs, history)."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Generator

from backer.server.models import Client, ClientStatus


class Storage:
    """SQLite-based storage for server data."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS clients (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    hostname TEXT NOT NULL,
                    ip_address TEXT,
                    status TEXT DEFAULT 'unknown',
                    last_seen TEXT,
                    registered_at TEXT NOT NULL,
                    version TEXT,
                    os_info TEXT,
                    tags TEXT DEFAULT '[]',
                    secret_hash TEXT
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    name TEXT PRIMARY KEY,
                    config TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS job_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    job_name TEXT NOT NULL,
                    client_id TEXT,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    bytes_transferred INTEGER DEFAULT 0,
                    files_transferred INTEGER DEFAULT 0,
                    errors TEXT DEFAULT '[]',
                    output TEXT DEFAULT '',
                    FOREIGN KEY (job_name) REFERENCES jobs(name)
                );

                CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs(job_name);
                CREATE INDEX IF NOT EXISTS idx_job_runs_started ON job_runs(started_at DESC);

                CREATE TABLE IF NOT EXISTS command_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id TEXT NOT NULL,
                    command_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    executed_at TEXT,
                    status TEXT DEFAULT 'pending',
                    FOREIGN KEY (client_id) REFERENCES clients(id)
                );

                CREATE INDEX IF NOT EXISTS idx_commands_client ON command_queue(client_id, status);

                CREATE TABLE IF NOT EXISTS repositories (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    repo_type TEXT NOT NULL,
                    server TEXT,
                    share TEXT,
                    path TEXT DEFAULT '',
                    username TEXT,
                    password_encrypted TEXT,
                    domain TEXT,
                    mount_point TEXT,
                    status TEXT DEFAULT 'disconnected',
                    last_checked TEXT,
                    created_at TEXT NOT NULL,
                    config TEXT DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_repositories_name ON repositories(name);

                CREATE TABLE IF NOT EXISTS job_progress (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    job_name TEXT NOT NULL,
                    client_id TEXT,
                    status TEXT DEFAULT 'starting',
                    progress_percent INTEGER DEFAULT 0,
                    current_file TEXT,
                    bytes_processed INTEGER DEFAULT 0,
                    files_processed INTEGER DEFAULT 0,
                    total_bytes INTEGER DEFAULT 0,
                    total_files INTEGER DEFAULT 0,
                    message TEXT,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (job_name) REFERENCES jobs(name)
                );

                CREATE INDEX IF NOT EXISTS idx_job_progress_run ON job_progress(run_id);
                CREATE INDEX IF NOT EXISTS idx_job_progress_status ON job_progress(status);
            """)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        """Get a database connection."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # Client operations
    def add_client(self, client: Client, secret_hash: str) -> None:
        """Register a new client."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO clients (id, name, hostname, ip_address, status,
                    last_seen, registered_at, version, os_info, tags, secret_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client.id,
                    client.name,
                    client.hostname,
                    client.ip_address,
                    client.status.value,
                    client.last_seen.isoformat() if client.last_seen else None,
                    client.registered_at.isoformat(),
                    client.version,
                    client.os_info,
                    json.dumps(client.tags),
                    secret_hash,
                ),
            )

    def get_client(self, client_id: str) -> Client | None:
        """Get a client by ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM clients WHERE id = ?", (client_id,)
            ).fetchone()
            if row:
                return self._row_to_client(row)
        return None

    def get_client_secret_hash(self, client_id: str) -> str | None:
        """Get client's secret hash for auth."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT secret_hash FROM clients WHERE id = ?", (client_id,)
            ).fetchone()
            return row["secret_hash"] if row else None

    def list_clients(self) -> list[Client]:
        """List all registered clients."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM clients ORDER BY name").fetchall()
            return [self._row_to_client(row) for row in rows]

    def update_client_status(
        self, client_id: str, status: ClientStatus, ip_address: str | None = None
    ) -> None:
        """Update client status and last seen time."""
        with self._connect() as conn:
            if ip_address:
                conn.execute(
                    """
                    UPDATE clients SET status = ?, last_seen = ?, ip_address = ?
                    WHERE id = ?
                    """,
                    (status.value, datetime.now().isoformat(), ip_address, client_id),
                )
            else:
                conn.execute(
                    "UPDATE clients SET status = ?, last_seen = ? WHERE id = ?",
                    (status.value, datetime.now().isoformat(), client_id),
                )

    def delete_client(self, client_id: str) -> bool:
        """Delete a client."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))
            return cursor.rowcount > 0

    def _row_to_client(self, row: sqlite3.Row) -> Client:
        """Convert a database row to a Client object."""
        return Client(
            id=row["id"],
            name=row["name"],
            hostname=row["hostname"],
            ip_address=row["ip_address"],
            status=ClientStatus(row["status"]),
            last_seen=datetime.fromisoformat(row["last_seen"]) if row["last_seen"] else None,
            registered_at=datetime.fromisoformat(row["registered_at"]),
            version=row["version"],
            os_info=row["os_info"],
            tags=json.loads(row["tags"]) if row["tags"] else [],
        )

    # Job operations
    def save_job(self, name: str, config: dict[str, Any]) -> None:
        """Save or update a job configuration."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (name, config, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET config = ?, updated_at = ?
                """,
                (name, json.dumps(config), now, now, json.dumps(config), now),
            )

    def get_job(self, name: str) -> dict[str, Any] | None:
        """Get a job configuration by name."""
        with self._connect() as conn:
            row = conn.execute("SELECT config FROM jobs WHERE name = ?", (name,)).fetchone()
            return json.loads(row["config"]) if row else None

    def list_jobs(self) -> list[dict[str, Any]]:
        """List all job configurations."""
        with self._connect() as conn:
            rows = conn.execute("SELECT name, config FROM jobs ORDER BY name").fetchall()
            return [{"name": row["name"], **json.loads(row["config"])} for row in rows]

    def delete_job(self, name: str) -> bool:
        """Delete a job."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM jobs WHERE name = ?", (name,))
            return cursor.rowcount > 0

    # Job run history
    def save_job_run(
        self,
        run_id: str,
        job_name: str,
        status: str,
        started_at: datetime,
        finished_at: datetime | None = None,
        client_id: str | None = None,
        bytes_transferred: int = 0,
        files_transferred: int = 0,
        errors: list[str] | None = None,
        output: str = "",
    ) -> None:
        """Save or update a job run record.

        Uses INSERT OR REPLACE to handle both new records and updates
        to existing pending records (when results come in).
        """
        with self._connect() as conn:
            # Check if a record with this run_id already exists
            existing = conn.execute(
                "SELECT id FROM job_runs WHERE run_id = ? AND job_name = ?",
                (run_id, job_name),
            ).fetchone()

            if existing:
                # Update existing record (e.g., pending -> completed)
                conn.execute(
                    """
                    UPDATE job_runs SET
                        status = ?,
                        finished_at = ?,
                        bytes_transferred = ?,
                        files_transferred = ?,
                        errors = ?,
                        output = ?
                    WHERE run_id = ? AND job_name = ?
                    """,
                    (
                        status,
                        finished_at.isoformat() if finished_at else None,
                        bytes_transferred,
                        files_transferred,
                        json.dumps(errors or []),
                        output,
                        run_id,
                        job_name,
                    ),
                )
            else:
                # Insert new record
                conn.execute(
                    """
                    INSERT INTO job_runs (run_id, job_name, client_id, status, started_at,
                        finished_at, bytes_transferred, files_transferred, errors, output)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        job_name,
                        client_id,
                        status,
                        started_at.isoformat(),
                        finished_at.isoformat() if finished_at else None,
                        bytes_transferred,
                        files_transferred,
                        json.dumps(errors or []),
                        output,
                    ),
                )

    def get_job_runs(self, job_name: str, limit: int = 20) -> list[dict[str, Any]]:
        """Get recent runs for a job."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM job_runs WHERE job_name = ?
                ORDER BY started_at DESC LIMIT ?
                """,
                (job_name, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_latest_run(self, job_name: str) -> dict[str, Any] | None:
        """Get the most recent run for a job."""
        runs = self.get_job_runs(job_name, limit=1)
        return runs[0] if runs else None

    # Command queue operations
    def queue_command(
        self,
        client_id: str,
        command_type: str,
        payload: dict[str, Any],
    ) -> int:
        """Add a command to the queue for a client.

        Returns the command ID.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO command_queue (client_id, command_type, payload, created_at, status)
                VALUES (?, ?, ?, ?, 'pending')
                """,
                (client_id, command_type, json.dumps(payload), datetime.now().isoformat()),
            )
            return cursor.lastrowid or 0

    def get_pending_commands(self, client_id: str) -> list[dict[str, Any]]:
        """Get pending commands for a client."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, command_type, payload, created_at FROM command_queue
                WHERE client_id = ? AND status = 'pending'
                ORDER BY created_at ASC
                """,
                (client_id,),
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "command": row["command_type"],
                    "payload": json.loads(row["payload"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    def mark_command_executed(self, command_id: int) -> None:
        """Mark a command as executed."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE command_queue SET status = 'executed', executed_at = ?
                WHERE id = ?
                """,
                (datetime.now().isoformat(), command_id),
            )

    def mark_command_failed(self, command_id: int) -> None:
        """Mark a command as failed."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE command_queue SET status = 'failed', executed_at = ?
                WHERE id = ?
                """,
                (datetime.now().isoformat(), command_id),
            )

    # Repository operations
    def add_repository(
        self,
        repo_id: str,
        name: str,
        repo_type: str,
        server: str | None = None,
        share: str | None = None,
        path: str = "",
        username: str | None = None,
        password_encrypted: str | None = None,
        domain: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        """Add a new storage repository."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO repositories (id, name, repo_type, server, share, path,
                    username, password_encrypted, domain, created_at, config)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo_id,
                    name,
                    repo_type,
                    server,
                    share,
                    path,
                    username,
                    password_encrypted,
                    domain,
                    datetime.now().isoformat(),
                    json.dumps(config or {}),
                ),
            )

    def get_repository(self, repo_id: str) -> dict[str, Any] | None:
        """Get a repository by ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM repositories WHERE id = ?", (repo_id,)
            ).fetchone()
            if row:
                return self._row_to_repository(row)
        return None

    def get_repository_by_name(self, name: str) -> dict[str, Any] | None:
        """Get a repository by name."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM repositories WHERE name = ?", (name,)
            ).fetchone()
            if row:
                return self._row_to_repository(row)
        return None

    def list_repositories(self) -> list[dict[str, Any]]:
        """List all repositories."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM repositories ORDER BY name"
            ).fetchall()
            return [self._row_to_repository(row) for row in rows]

    def update_repository_status(
        self,
        repo_id: str,
        status: str,
        mount_point: str | None = None,
    ) -> None:
        """Update repository connection status."""
        with self._connect() as conn:
            if mount_point:
                conn.execute(
                    """
                    UPDATE repositories SET status = ?, mount_point = ?, last_checked = ?
                    WHERE id = ?
                    """,
                    (status, mount_point, datetime.now().isoformat(), repo_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE repositories SET status = ?, last_checked = ?
                    WHERE id = ?
                    """,
                    (status, datetime.now().isoformat(), repo_id),
                )

    def delete_repository(self, repo_id: str) -> bool:
        """Delete a repository."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM repositories WHERE id = ?", (repo_id,))
            return cursor.rowcount > 0

    def _row_to_repository(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a database row to a repository dict."""
        return {
            "id": row["id"],
            "name": row["name"],
            "repo_type": row["repo_type"],
            "server": row["server"],
            "share": row["share"],
            "path": row["path"],
            "username": row["username"],
            "has_password": bool(row["password_encrypted"]),
            "domain": row["domain"],
            "mount_point": row["mount_point"],
            "status": row["status"],
            "last_checked": row["last_checked"],
            "created_at": row["created_at"],
            "config": json.loads(row["config"]) if row["config"] else {},
        }

    def get_repository_password(self, repo_id: str) -> str | None:
        """Get repository password (for internal use only)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT password_encrypted FROM repositories WHERE id = ?", (repo_id,)
            ).fetchone()
            # For now, just base64 - in production use proper encryption
            if row and row["password_encrypted"]:
                import base64
                try:
                    return base64.b64decode(row["password_encrypted"]).decode()
                except Exception:
                    return None
        return None

    def set_repository_password(self, repo_id: str, password: str) -> None:
        """Set repository password."""
        import base64
        # For now, just base64 - in production use proper encryption
        encrypted = base64.b64encode(password.encode()).decode()
        with self._connect() as conn:
            conn.execute(
                "UPDATE repositories SET password_encrypted = ? WHERE id = ?",
                (encrypted, repo_id),
            )

    # Progress tracking operations
    def start_job_progress(
        self,
        run_id: str,
        job_name: str,
        client_id: str | None = None,
    ) -> None:
        """Start tracking progress for a job run."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO job_progress
                    (run_id, job_name, client_id, status, started_at, updated_at)
                VALUES (?, ?, ?, 'starting', ?, ?)
                """,
                (run_id, job_name, client_id, now, now),
            )

    def update_job_progress(
        self,
        run_id: str,
        status: str | None = None,
        progress_percent: int | None = None,
        current_file: str | None = None,
        bytes_processed: int | None = None,
        files_processed: int | None = None,
        total_bytes: int | None = None,
        total_files: int | None = None,
        message: str | None = None,
    ) -> None:
        """Update progress for a running job."""
        updates = ["updated_at = ?"]
        params: list[Any] = [datetime.now().isoformat()]

        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if progress_percent is not None:
            updates.append("progress_percent = ?")
            params.append(progress_percent)
        if current_file is not None:
            updates.append("current_file = ?")
            params.append(current_file)
        if bytes_processed is not None:
            updates.append("bytes_processed = ?")
            params.append(bytes_processed)
        if files_processed is not None:
            updates.append("files_processed = ?")
            params.append(files_processed)
        if total_bytes is not None:
            updates.append("total_bytes = ?")
            params.append(total_bytes)
        if total_files is not None:
            updates.append("total_files = ?")
            params.append(total_files)
        if message is not None:
            updates.append("message = ?")
            params.append(message)

        params.append(run_id)

        with self._connect() as conn:
            conn.execute(
                f"UPDATE job_progress SET {', '.join(updates)} WHERE run_id = ?",
                params,
            )

    def finish_job_progress(self, run_id: str, status: str = "completed") -> None:
        """Mark a job progress as finished and clean up."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE job_progress SET status = ?, progress_percent = 100, updated_at = ?
                WHERE run_id = ?
                """,
                (status, datetime.now().isoformat(), run_id),
            )

    def get_job_progress(self, run_id: str) -> dict[str, Any] | None:
        """Get progress for a specific job run."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM job_progress WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row:
                return dict(row)
        return None

    def get_running_jobs(self) -> list[dict[str, Any]]:
        """Get all currently running jobs."""
        with self._connect() as conn:
            # Get from job_progress table - active jobs
            rows = conn.execute(
                """
                SELECT * FROM job_progress
                WHERE status NOT IN ('completed', 'failed', 'cancelled')
                ORDER BY started_at DESC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def cleanup_stale_progress(self, max_age_minutes: int = 60) -> int:
        """Clean up stale progress records (jobs that hung or crashed)."""
        cutoff = (datetime.now() - timedelta(minutes=max_age_minutes)).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM job_progress
                WHERE status NOT IN ('completed', 'failed', 'cancelled')
                AND updated_at < ?
                """,
                (cutoff,),
            )
            return cursor.rowcount
