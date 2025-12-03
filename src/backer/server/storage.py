"""Storage backend for server state (clients, jobs, history)."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
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
        """Save a job run record."""
        with self._connect() as conn:
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
