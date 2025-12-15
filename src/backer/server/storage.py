"""Storage backend for server state (clients, jobs, history)."""

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backer.server.models import Client, ClientStatus


class Storage:
    """SQLite-based storage for server data."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path) if isinstance(db_path, str) else db_path
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
                    snapshot_id TEXT,
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

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS browse_requests (
                    request_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    path TEXT DEFAULT '',
                    status TEXT DEFAULT 'pending',
                    entries TEXT DEFAULT '[]',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (client_id) REFERENCES clients(id)
                );

                CREATE INDEX IF NOT EXISTS idx_browse_requests_status ON browse_requests(status);

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    email TEXT,
                    role TEXT DEFAULT 'admin',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

                -- Hypervisors table for Proxmox, VMware, etc.
                CREATE TABLE IF NOT EXISTS hypervisors (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    hypervisor_type TEXT NOT NULL,  -- proxmox, vmware, hyper-v
                    host TEXT NOT NULL,
                    port INTEGER DEFAULT 8006,
                    auth_method TEXT DEFAULT 'token',  -- token, password
                    username TEXT,
                    token_id TEXT,
                    token_secret_encrypted TEXT,
                    password_encrypted TEXT,
                    verify_ssl INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'unknown',
                    version TEXT,
                    last_checked TEXT,
                    created_at TEXT NOT NULL,
                    config TEXT DEFAULT '{}',
                    -- SSH settings for incremental backups (QMP access)
                    ssh_user TEXT DEFAULT 'root',
                    ssh_port INTEGER DEFAULT 22,
                    ssh_key_path TEXT,  -- Path to SSH private key (optional)
                    ssh_use_api_password INTEGER DEFAULT 1  -- If 1, use API password for SSH (password auth only)
                );

                CREATE INDEX IF NOT EXISTS idx_hypervisors_name ON hypervisors(name);
                CREATE INDEX IF NOT EXISTS idx_hypervisors_type ON hypervisors(hypervisor_type);

                -- Hypervisor backup jobs
                CREATE TABLE IF NOT EXISTS hypervisor_jobs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    hypervisor_id TEXT NOT NULL,
                    guest_ids TEXT NOT NULL,  -- JSON array of VMIDs
                    repository_id TEXT NOT NULL,  -- Backer repository for backups
                    backup_mode TEXT DEFAULT 'snapshot',
                    compression TEXT DEFAULT 'zstd',
                    schedule_cron TEXT,
                    retention TEXT DEFAULT '{}',  -- JSON retention policy
                    enabled INTEGER DEFAULT 1,
                    copies_to_keep INTEGER DEFAULT 0,  -- Number of backup copies to keep (0 = unlimited)
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (hypervisor_id) REFERENCES hypervisors(id),
                    FOREIGN KEY (repository_id) REFERENCES repositories(id)
                );

                CREATE INDEX IF NOT EXISTS idx_hypervisor_jobs_hypervisor ON hypervisor_jobs(hypervisor_id);

                -- Hypervisor backup runs
                CREATE TABLE IF NOT EXISTS hypervisor_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    job_name TEXT NOT NULL,
                    hypervisor_id TEXT NOT NULL,
                    guest_id INTEGER NOT NULL,
                    guest_name TEXT,
                    guest_type TEXT DEFAULT 'qemu',
                    status TEXT NOT NULL,
                    upid TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    duration_seconds REAL,
                    backup_size INTEGER DEFAULT 0,
                    backup_filename TEXT,
                    exit_status TEXT,
                    errors TEXT DEFAULT '[]',
                    FOREIGN KEY (job_id) REFERENCES hypervisor_jobs(id),
                    FOREIGN KEY (hypervisor_id) REFERENCES hypervisors(id)
                );

                CREATE INDEX IF NOT EXISTS idx_hypervisor_runs_job ON hypervisor_runs(job_id);
                CREATE INDEX IF NOT EXISTS idx_hypervisor_runs_started ON hypervisor_runs(started_at DESC);

                -- VM bitmap state tracking for incremental backups
                -- Tracks dirty bitmap state per VM disk to enable incremental backups
                CREATE TABLE IF NOT EXISTS vm_bitmap_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hypervisor_id TEXT NOT NULL,
                    vmid INTEGER NOT NULL,
                    disk_node TEXT NOT NULL,  -- Block device node (e.g., "drive-scsi0")
                    disk_driver TEXT NOT NULL,  -- Disk format (qcow2, raw, etc.)
                    bitmap_name TEXT NOT NULL,  -- Name of the dirty bitmap
                    bitmap_valid INTEGER DEFAULT 1,  -- Whether bitmap is usable
                    last_full_backup TEXT,  -- Timestamp of last full backup
                    last_incremental_backup TEXT,  -- Timestamp of last incremental
                    backup_count INTEGER DEFAULT 0,  -- Number of incrementals since full
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (hypervisor_id) REFERENCES hypervisors(id),
                    UNIQUE(hypervisor_id, vmid, disk_node)
                );

                CREATE INDEX IF NOT EXISTS idx_vm_bitmap_state_hv ON vm_bitmap_state(hypervisor_id);
                CREATE INDEX IF NOT EXISTS idx_vm_bitmap_state_vm ON vm_bitmap_state(hypervisor_id, vmid);
            """)

            # Migration: Add copies_to_keep column if it doesn't exist
            try:
                conn.execute(
                    "ALTER TABLE hypervisor_jobs ADD COLUMN copies_to_keep "
                    "INTEGER DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Migration: Convert delete_before_backup to copies_to_keep
            # If delete_before_backup was 1, set copies_to_keep to 1
            try:
                conn.execute(
                    """
                    UPDATE hypervisor_jobs
                    SET copies_to_keep = 1
                    WHERE delete_before_backup = 1 AND copies_to_keep = 0
                    """
                )
            except sqlite3.OperationalError:
                pass  # delete_before_backup column doesn't exist (fresh install)

            # Migration: Add backup_size column to hypervisor_runs if it doesn't exist
            try:
                conn.execute(
                    "ALTER TABLE hypervisor_runs ADD COLUMN backup_size "
                    "INTEGER DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Migration: Add guest_type column to hypervisor_runs if it doesn't exist
            try:
                conn.execute(
                    "ALTER TABLE hypervisor_runs ADD COLUMN guest_type "
                    "TEXT DEFAULT 'qemu'"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Migration: Add backup_filename column to hypervisor_runs if it doesn't exist
            try:
                conn.execute(
                    "ALTER TABLE hypervisor_runs ADD COLUMN backup_filename TEXT"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        """Get a database connection."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)  # 30 second timeout for locks
        conn.row_factory = sqlite3.Row
        # Enable Write-Ahead Logging for better concurrent access
        conn.execute("PRAGMA journal_mode=WAL")
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

    def get_client_by_hostname(self, hostname: str) -> Client | None:
        """Get a client by hostname."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM clients WHERE hostname = ?", (hostname,)
            ).fetchone()
            if row:
                return self._row_to_client(row)
        return None

    def update_client_secret(self, client_id: str, secret_hash: str) -> None:
        """Update a client's secret hash."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE clients SET secret_hash = ? WHERE id = ?",
                (secret_hash, client_id),
            )

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
        """Delete a job and all associated records.

        This cleans up:
        - The job configuration from the jobs table
        - All job_progress records (running/pending jobs)
        - All job_runs history records
        """
        with self._connect() as conn:
            # Delete associated progress records first (prevents orphaned running jobs)
            conn.execute("DELETE FROM job_progress WHERE job_name = ?", (name,))

            # Delete job run history
            conn.execute("DELETE FROM job_runs WHERE job_name = ?", (name,))

            # Delete the job itself
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
        snapshot_id: str | None = None,
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
                        output = ?,
                        snapshot_id = ?
                    WHERE run_id = ? AND job_name = ?
                    """,
                    (
                        status,
                        finished_at.isoformat() if finished_at else None,
                        bytes_transferred,
                        files_transferred,
                        json.dumps(errors or []),
                        output,
                        snapshot_id,
                        run_id,
                        job_name,
                    ),
                )
            else:
                # Insert new record
                conn.execute(
                    """
                    INSERT INTO job_runs (run_id, job_name, client_id, status, started_at,
                        finished_at, bytes_transferred, files_transferred, errors, output, snapshot_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        snapshot_id,
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

    def get_job_run(self, run_id: str) -> dict[str, Any] | None:
        """Get a specific job run by its run_id."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM job_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_all_job_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get recent runs across all jobs."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM job_runs
                ORDER BY started_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

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
                    "command_type": row["command_type"],
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

    def clear_pending_commands(self, client_id: str | None = None) -> int:
        """Clear all pending commands, optionally for a specific client.

        Returns the number of commands cleared.
        """
        with self._connect() as conn:
            if client_id:
                cursor = conn.execute(
                    """
                    UPDATE command_queue SET status = 'cancelled', executed_at = ?
                    WHERE client_id = ? AND status = 'pending'
                    """,
                    (datetime.now().isoformat(), client_id),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE command_queue SET status = 'cancelled', executed_at = ?
                    WHERE status = 'pending'
                    """,
                    (datetime.now().isoformat(),),
                )
            return cursor.rowcount

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
        """Get repository password (for internal use only).

        Handles both new Fernet-encrypted passwords and legacy base64 passwords.
        Legacy passwords are automatically migrated to encrypted format.
        """
        from backer.server.secrets import get_secrets_manager

        with self._connect() as conn:
            row = conn.execute(
                "SELECT password_encrypted FROM repositories WHERE id = ?", (repo_id,)
            ).fetchone()

            if not row or not row["password_encrypted"]:
                return None

            encrypted_value = row["password_encrypted"]
            secrets = get_secrets_manager(self.db_path.parent)

            # Check if it's Fernet-encrypted (new format)
            if secrets.is_encrypted(encrypted_value):
                return secrets.decrypt(encrypted_value)

            # Legacy base64 format - migrate to encrypted
            import base64
            try:
                plaintext = base64.b64decode(encrypted_value).decode()
                # Migrate to new encrypted format
                new_encrypted = secrets.encrypt(plaintext)
                conn.execute(
                    "UPDATE repositories SET password_encrypted = ? WHERE id = ?",
                    (new_encrypted, repo_id),
                )
                return plaintext
            except Exception:
                return None

    def set_repository_password(self, repo_id: str, password: str) -> None:
        """Set repository password using Fernet encryption."""
        from backer.server.secrets import get_secrets_manager

        secrets = get_secrets_manager(self.db_path.parent)
        encrypted = secrets.encrypt(password)

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

    # Settings operations
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        """Get a setting value by key."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        """Set a setting value."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?
                """,
                (key, value, now, value, now),
            )

    def get_all_settings(self) -> dict[str, str]:
        """Get all settings as a dictionary."""
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
            return {row["key"]: row["value"] for row in rows}

    # Browse request operations
    def save_browse_request(
        self,
        request_id: str,
        client_id: str,
        path: str,
    ) -> None:
        """Save a new browse request."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO browse_requests
                    (request_id, client_id, path, status, created_at, updated_at)
                VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (request_id, client_id, path, now, now),
            )

    def save_browse_result(
        self,
        request_id: str,
        status: str,
        entries: list[dict[str, Any]],
        error: str | None = None,
        path: str | None = None,
    ) -> None:
        """Save browse results from agent."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            if path is not None:
                conn.execute(
                    """
                    UPDATE browse_requests
                    SET status = ?, entries = ?, error = ?, path = ?, updated_at = ?
                    WHERE request_id = ?
                    """,
                    (status, json.dumps(entries), error, path, now, request_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE browse_requests
                    SET status = ?, entries = ?, error = ?, updated_at = ?
                    WHERE request_id = ?
                    """,
                    (status, json.dumps(entries), error, now, request_id),
                )

    def get_browse_result(self, request_id: str) -> dict[str, Any] | None:
        """Get browse request result."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM browse_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row:
                return {
                    "request_id": row["request_id"],
                    "client_id": row["client_id"],
                    "path": row["path"],
                    "status": row["status"],
                    "entries": json.loads(row["entries"]) if row["entries"] else [],
                    "error": row["error"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
        return None

    def cleanup_old_browse_requests(self, max_age_minutes: int = 10) -> int:
        """Clean up old browse requests."""
        cutoff = (datetime.now() - timedelta(minutes=max_age_minutes)).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM browse_requests WHERE created_at < ?",
                (cutoff,),
            )
            return cursor.rowcount

    # User management operations
    def create_user(
        self,
        username: str,
        password_hash: str,
        display_name: str,
        email: str | None = None,
        role: str = "admin",
    ) -> int:
        """Create a new user. Returns user ID."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO users (username, password_hash, display_name, email, role, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (username, password_hash, display_name, email, role, now, now),
            )
            return cursor.lastrowid or 0

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        """Get a user by ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if row:
                return self._row_to_user(row)
        return None

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        """Get a user by username."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
            if row:
                return self._row_to_user(row)
        return None

    def update_user(
        self,
        user_id: int,
        display_name: str | None = None,
        email: str | None = None,
        password_hash: str | None = None,
    ) -> bool:
        """Update user information."""
        updates = ["updated_at = ?"]
        params: list[Any] = [datetime.now().isoformat()]

        if display_name is not None:
            updates.append("display_name = ?")
            params.append(display_name)
        if email is not None:
            updates.append("email = ?")
            params.append(email)
        if password_hash is not None:
            updates.append("password_hash = ?")
            params.append(password_hash)

        params.append(user_id)

        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE users SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            return cursor.rowcount > 0

    def update_last_login(self, user_id: int) -> None:
        """Update user's last login timestamp."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET last_login = ? WHERE id = ?",
                (datetime.now().isoformat(), user_id),
            )

    def list_users(self) -> list[dict[str, Any]]:
        """List all users."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY username"
            ).fetchall()
            return [self._row_to_user(row) for row in rows]

    def count_users(self) -> int:
        """Count total number of users."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) as count FROM users").fetchone()
            return row["count"] if row else 0

    def _row_to_user(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a database row to a user dict."""
        return {
            "id": row["id"],
            "username": row["username"],
            "password_hash": row["password_hash"],
            "display_name": row["display_name"],
            "email": row["email"],
            "role": row["role"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_login": row["last_login"],
        }

    # =========================================================================
    # Hypervisor Operations
    # =========================================================================

    def add_hypervisor(
        self,
        hypervisor_id: str,
        name: str,
        hypervisor_type: str,
        host: str,
        port: int = 8006,
        auth_method: str = "token",
        username: str | None = None,
        token_id: str | None = None,
        token_secret: str | None = None,
        password: str | None = None,
        verify_ssl: bool = False,
        config: dict[str, Any] | None = None,
        ssh_user: str = "root",
        ssh_port: int = 22,
        ssh_key_path: str | None = None,
        ssh_use_api_password: bool = True,
    ) -> None:
        """Add a new hypervisor."""
        from backer.server.secrets import get_secrets_manager

        secrets = get_secrets_manager(self.db_path.parent)
        now = datetime.now().isoformat()

        # Encrypt sensitive credentials
        token_secret_encrypted = None
        password_encrypted = None

        if token_secret:
            token_secret_encrypted = secrets.encrypt(token_secret)
        if password:
            password_encrypted = secrets.encrypt(password)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO hypervisors (
                    id, name, hypervisor_type, host, port, auth_method,
                    username, token_id, token_secret_encrypted, password_encrypted,
                    verify_ssl, status, created_at, config,
                    ssh_user, ssh_port, ssh_key_path, ssh_use_api_password
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?, ?, ?, ?, ?, ?)
                """,
                (
                    hypervisor_id,
                    name,
                    hypervisor_type,
                    host,
                    port,
                    auth_method,
                    username,
                    token_id,
                    token_secret_encrypted,
                    password_encrypted,
                    1 if verify_ssl else 0,
                    now,
                    json.dumps(config or {}),
                    ssh_user,
                    ssh_port,
                    ssh_key_path,
                    1 if ssh_use_api_password else 0,
                ),
            )

    def update_hypervisor(
        self,
        hypervisor_id: str,
        name: str | None = None,
        host: str | None = None,
        port: int | None = None,
        auth_method: str | None = None,
        username: str | None = None,
        token_id: str | None = None,
        token_secret: str | None = None,
        password: str | None = None,
        verify_ssl: bool | None = None,
        status: str | None = None,
        version: str | None = None,
        config: dict[str, Any] | None = None,
        ssh_user: str | None = None,
        ssh_port: int | None = None,
        ssh_key_path: str | None = None,
        ssh_use_api_password: bool | None = None,
    ) -> bool:
        """Update a hypervisor."""
        from backer.server.secrets import get_secrets_manager

        updates = []
        params: list[Any] = []

        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if host is not None:
            updates.append("host = ?")
            params.append(host)
        if port is not None:
            updates.append("port = ?")
            params.append(port)
        if auth_method is not None:
            updates.append("auth_method = ?")
            params.append(auth_method)
        if username is not None:
            updates.append("username = ?")
            params.append(username)
        if token_id is not None:
            updates.append("token_id = ?")
            params.append(token_id)
        if token_secret is not None:
            secrets = get_secrets_manager(self.db_path.parent)
            updates.append("token_secret_encrypted = ?")
            params.append(secrets.encrypt(token_secret))
        if password is not None:
            secrets = get_secrets_manager(self.db_path.parent)
            updates.append("password_encrypted = ?")
            params.append(secrets.encrypt(password))
        if verify_ssl is not None:
            updates.append("verify_ssl = ?")
            params.append(1 if verify_ssl else 0)
        if status is not None:
            updates.append("status = ?")
            params.append(status)
            updates.append("last_checked = ?")
            params.append(datetime.now().isoformat())
        if version is not None:
            updates.append("version = ?")
            params.append(version)
        if config is not None:
            updates.append("config = ?")
            params.append(json.dumps(config))
        # SSH settings for incremental backups
        if ssh_user is not None:
            updates.append("ssh_user = ?")
            params.append(ssh_user)
        if ssh_port is not None:
            updates.append("ssh_port = ?")
            params.append(ssh_port)
        if ssh_key_path is not None:
            updates.append("ssh_key_path = ?")
            params.append(ssh_key_path if ssh_key_path else None)
        if ssh_use_api_password is not None:
            updates.append("ssh_use_api_password = ?")
            params.append(1 if ssh_use_api_password else 0)

        if not updates:
            return False

        params.append(hypervisor_id)

        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE hypervisors SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            return cursor.rowcount > 0

    def get_hypervisor(self, hypervisor_id: str) -> dict[str, Any] | None:
        """Get a hypervisor by ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM hypervisors WHERE id = ?", (hypervisor_id,)
            ).fetchone()
            if row:
                return self._row_to_hypervisor(row)
        return None

    def get_hypervisor_by_name(self, name: str) -> dict[str, Any] | None:
        """Get a hypervisor by name."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM hypervisors WHERE name = ?", (name,)
            ).fetchone()
            if row:
                return self._row_to_hypervisor(row)
        return None

    def list_hypervisors(self, hypervisor_type: str | None = None) -> list[dict[str, Any]]:
        """List all hypervisors, optionally filtered by type."""
        with self._connect() as conn:
            if hypervisor_type:
                rows = conn.execute(
                    "SELECT * FROM hypervisors WHERE hypervisor_type = ? ORDER BY name",
                    (hypervisor_type,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM hypervisors ORDER BY name"
                ).fetchall()
            return [self._row_to_hypervisor(row) for row in rows]

    def delete_hypervisor(self, hypervisor_id: str) -> bool:
        """Delete a hypervisor."""
        with self._connect() as conn:
            # First delete associated jobs and runs
            conn.execute(
                "DELETE FROM hypervisor_runs WHERE hypervisor_id = ?",
                (hypervisor_id,)
            )
            conn.execute(
                "DELETE FROM hypervisor_jobs WHERE hypervisor_id = ?",
                (hypervisor_id,)
            )
            cursor = conn.execute(
                "DELETE FROM hypervisors WHERE id = ?",
                (hypervisor_id,)
            )
            return cursor.rowcount > 0

    def get_hypervisor_token_secret(self, hypervisor_id: str) -> str | None:
        """Get decrypted token secret for a hypervisor."""
        from backer.server.secrets import get_secrets_manager

        with self._connect() as conn:
            row = conn.execute(
                "SELECT token_secret_encrypted FROM hypervisors WHERE id = ?",
                (hypervisor_id,),
            ).fetchone()
            if not row or not row["token_secret_encrypted"]:
                return None

            secrets = get_secrets_manager(self.db_path.parent)
            return secrets.decrypt(row["token_secret_encrypted"])

    def get_hypervisor_password(self, hypervisor_id: str) -> str | None:
        """Get decrypted password for a hypervisor."""
        from backer.server.secrets import get_secrets_manager

        with self._connect() as conn:
            row = conn.execute(
                "SELECT password_encrypted FROM hypervisors WHERE id = ?",
                (hypervisor_id,),
            ).fetchone()
            if not row or not row["password_encrypted"]:
                return None

            secrets = get_secrets_manager(self.db_path.parent)
            return secrets.decrypt(row["password_encrypted"])

    def _row_to_hypervisor(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a database row to a hypervisor dict."""
        # Handle optional SSH columns that may not exist in older databases
        keys = row.keys()
        config = json.loads(row["config"]) if row["config"] else {}
        result = {
            "id": row["id"],
            "name": row["name"],
            "hypervisor_type": row["hypervisor_type"],
            "host": row["host"],
            "port": row["port"],
            "auth_method": row["auth_method"],
            "username": row["username"],
            "token_id": row["token_id"],
            "verify_ssl": row["verify_ssl"] == 1,
            "status": row["status"],
            "version": row["version"],
            "last_checked": row["last_checked"],
            "created_at": row["created_at"],
            "config": config,
            # SSH settings for incremental backups
            "ssh_user": row["ssh_user"] if "ssh_user" in keys else "root",
            "ssh_port": row["ssh_port"] if "ssh_port" in keys else 22,
            "ssh_key_path": row["ssh_key_path"] if "ssh_key_path" in keys else None,
            "ssh_use_api_password": row["ssh_use_api_password"] == 1 if "ssh_use_api_password" in keys else True,
        }
        # Expose domain from config for Hyper-V
        if config.get("domain"):
            result["domain"] = config["domain"]
        return result

    # =========================================================================
    # Hypervisor Job Operations
    # =========================================================================

    def add_hypervisor_job(
        self,
        job_id: str,
        name: str,
        hypervisor_id: str,
        guest_ids: list[int | str],
        repository_id: str,
        backup_mode: str = "snapshot",
        compression: str = "zstd",
        schedule_cron: str | None = None,
        retention: dict[str, int] | None = None,
        enabled: bool = True,
        copies_to_keep: int = 0,
    ) -> None:
        """Add a new hypervisor backup job."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO hypervisor_jobs (
                    id, name, hypervisor_id, guest_ids, repository_id,
                    backup_mode, compression, schedule_cron, retention,
                    enabled, copies_to_keep, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    name,
                    hypervisor_id,
                    json.dumps(guest_ids),
                    repository_id,
                    backup_mode,
                    compression,
                    schedule_cron,
                    json.dumps(retention or {}),
                    1 if enabled else 0,
                    copies_to_keep,
                    now,
                    now,
                ),
            )

    def update_hypervisor_job(
        self,
        job_id: str,
        name: str | None = None,
        guest_ids: list[int | str] | None = None,
        repository_id: str | None = None,
        backup_mode: str | None = None,
        compression: str | None = None,
        schedule_cron: str | None = None,
        retention: dict[str, int] | None = None,
        enabled: bool | None = None,
        copies_to_keep: int | None = None,
    ) -> bool:
        """Update a hypervisor job."""
        updates = ["updated_at = ?"]
        params: list[Any] = [datetime.now().isoformat()]

        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if guest_ids is not None:
            updates.append("guest_ids = ?")
            params.append(json.dumps(guest_ids))
        if repository_id is not None:
            updates.append("repository_id = ?")
            params.append(repository_id)
        if backup_mode is not None:
            updates.append("backup_mode = ?")
            params.append(backup_mode)
        if compression is not None:
            updates.append("compression = ?")
            params.append(compression)
        if schedule_cron is not None:
            updates.append("schedule_cron = ?")
            params.append(schedule_cron if schedule_cron else None)
        if retention is not None:
            updates.append("retention = ?")
            params.append(json.dumps(retention))
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if enabled else 0)
        if copies_to_keep is not None:
            updates.append("copies_to_keep = ?")
            params.append(copies_to_keep)

        params.append(job_id)

        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE hypervisor_jobs SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            return cursor.rowcount > 0

    def get_hypervisor_job(self, job_id: str) -> dict[str, Any] | None:
        """Get a hypervisor job by ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM hypervisor_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row:
                return self._row_to_hypervisor_job(row)
        return None

    def get_hypervisor_job_by_name(self, name: str) -> dict[str, Any] | None:
        """Get a hypervisor job by name."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM hypervisor_jobs WHERE name = ?", (name,)
            ).fetchone()
            if row:
                return self._row_to_hypervisor_job(row)
        return None

    def list_hypervisor_jobs(self, hypervisor_id: str | None = None) -> list[dict[str, Any]]:
        """List hypervisor jobs, optionally filtered by hypervisor."""
        with self._connect() as conn:
            if hypervisor_id:
                rows = conn.execute(
                    "SELECT * FROM hypervisor_jobs WHERE hypervisor_id = ? ORDER BY name",
                    (hypervisor_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM hypervisor_jobs ORDER BY name"
                ).fetchall()
            return [self._row_to_hypervisor_job(row) for row in rows]

    def delete_hypervisor_job(self, job_id: str) -> bool:
        """Delete a hypervisor job."""
        with self._connect() as conn:
            # First delete associated runs
            conn.execute(
                "DELETE FROM hypervisor_runs WHERE job_id = ?",
                (job_id,)
            )
            cursor = conn.execute(
                "DELETE FROM hypervisor_jobs WHERE id = ?",
                (job_id,)
            )
            return cursor.rowcount > 0

    def _row_to_hypervisor_job(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a database row to a hypervisor job dict."""
        # Handle optional new columns that may not exist in older databases
        keys = row.keys()
        return {
            "id": row["id"],
            "name": row["name"],
            "hypervisor_id": row["hypervisor_id"],
            "guest_ids": json.loads(row["guest_ids"]) if row["guest_ids"] else [],
            "repository_id": row["repository_id"],
            "backup_mode": row["backup_mode"],
            "compression": row["compression"],
            "schedule_cron": row["schedule_cron"],
            "retention": json.loads(row["retention"]) if row["retention"] else {},
            "enabled": row["enabled"] == 1,
            "copies_to_keep": row["copies_to_keep"] if "copies_to_keep" in keys else 0,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # =========================================================================
    # Hypervisor Run Operations
    # =========================================================================

    def save_hypervisor_run(
        self,
        run_id: str,
        job_id: str,
        job_name: str,
        hypervisor_id: str,
        guest_id: int,
        guest_name: str | None = None,
        guest_type: str = "qemu",
        status: str = "pending",
        upid: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        duration_seconds: float | None = None,
        backup_size: int = 0,
        backup_filename: str | None = None,
        exit_status: str | None = None,
        errors: list[str] | None = None,
    ) -> None:
        """Save or update a hypervisor backup run."""
        started = (started_at or datetime.now()).isoformat()

        with self._connect() as conn:
            # Check if run exists
            existing = conn.execute(
                "SELECT id FROM hypervisor_runs WHERE run_id = ? AND guest_id = ?",
                (run_id, guest_id)
            ).fetchone()

            if existing:
                # Update
                conn.execute(
                    """
                    UPDATE hypervisor_runs SET
                        status = ?, upid = ?, finished_at = ?,
                        duration_seconds = ?, backup_size = ?, backup_filename = ?,
                        exit_status = ?, errors = ?
                    WHERE run_id = ? AND guest_id = ?
                    """,
                    (
                        status,
                        upid,
                        finished_at.isoformat() if finished_at else None,
                        duration_seconds,
                        backup_size,
                        backup_filename,
                        exit_status,
                        json.dumps(errors or []),
                        run_id,
                        guest_id,
                    ),
                )
            else:
                # Insert
                conn.execute(
                    """
                    INSERT INTO hypervisor_runs (
                        run_id, job_id, job_name, hypervisor_id, guest_id,
                        guest_name, guest_type, status, upid, started_at, finished_at,
                        duration_seconds, backup_size, backup_filename, exit_status, errors
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        job_id,
                        job_name,
                        hypervisor_id,
                        guest_id,
                        guest_name,
                        guest_type,
                        status,
                        upid,
                        started,
                        finished_at.isoformat() if finished_at else None,
                        duration_seconds,
                        backup_size,
                        backup_filename,
                        exit_status,
                        json.dumps(errors or []),
                    ),
                )

    def get_hypervisor_runs(
        self,
        job_id: str | None = None,
        hypervisor_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get hypervisor backup runs."""
        with self._connect() as conn:
            if job_id:
                rows = conn.execute(
                    """
                    SELECT * FROM hypervisor_runs
                    WHERE job_id = ?
                    ORDER BY started_at DESC
                    LIMIT ?
                    """,
                    (job_id, limit)
                ).fetchall()
            elif hypervisor_id:
                rows = conn.execute(
                    """
                    SELECT * FROM hypervisor_runs
                    WHERE hypervisor_id = ?
                    ORDER BY started_at DESC
                    LIMIT ?
                    """,
                    (hypervisor_id, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM hypervisor_runs
                    ORDER BY started_at DESC
                    LIMIT ?
                    """,
                    (limit,)
                ).fetchall()

            return [self._row_to_hypervisor_run(row) for row in rows]

    def get_latest_hypervisor_run(self, job_id: str) -> dict[str, Any] | None:
        """Get the latest run for a hypervisor job."""
        runs = self.get_hypervisor_runs(job_id=job_id, limit=1)
        return runs[0] if runs else None

    def get_hypervisor_run_by_run_id(self, run_id: str) -> dict[str, Any] | None:
        """Get a specific hypervisor run by its run_id."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM hypervisor_runs WHERE run_id = ? LIMIT 1",
                (run_id,)
            ).fetchone()
            if row:
                return self._row_to_hypervisor_run(row)
            return None

    def _row_to_hypervisor_run(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a database row to a hypervisor run dict."""
        keys = row.keys()
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "job_id": row["job_id"],
            "job_name": row["job_name"],
            "hypervisor_id": row["hypervisor_id"],
            "guest_id": row["guest_id"],
            "guest_name": row["guest_name"],
            "guest_type": row["guest_type"] if "guest_type" in keys else "qemu",
            "status": row["status"],
            "upid": row["upid"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "duration_seconds": row["duration_seconds"],
            "backup_size": row["backup_size"] if "backup_size" in keys else 0,
            "backup_filename": row["backup_filename"] if "backup_filename" in keys else None,
            "exit_status": row["exit_status"],
            "errors": json.loads(row["errors"]) if row["errors"] else [],
        }

    # =========================================================================
    # VM Bitmap State (for incremental backups)
    # =========================================================================

    def get_vm_bitmap_state(
        self,
        hypervisor_id: str,
        vmid: int,
        disk_node: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get bitmap state for a VM's disks.

        Args:
            hypervisor_id: Hypervisor ID
            vmid: VM ID
            disk_node: Optional specific disk node to query

        Returns:
            List of bitmap state dicts
        """
        with self._connect() as conn:
            if disk_node:
                rows = conn.execute(
                    """
                    SELECT * FROM vm_bitmap_state
                    WHERE hypervisor_id = ? AND vmid = ? AND disk_node = ?
                    """,
                    (hypervisor_id, vmid, disk_node),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM vm_bitmap_state
                    WHERE hypervisor_id = ? AND vmid = ?
                    """,
                    (hypervisor_id, vmid),
                ).fetchall()

            return [self._row_to_bitmap_state(row) for row in rows]

    def save_vm_bitmap_state(
        self,
        hypervisor_id: str,
        vmid: int,
        disk_node: str,
        disk_driver: str,
        bitmap_name: str,
        bitmap_valid: bool = True,
        last_full_backup: str | None = None,
        last_incremental_backup: str | None = None,
        backup_count: int = 0,
    ) -> None:
        """Save or update bitmap state for a VM disk.

        Args:
            hypervisor_id: Hypervisor ID
            vmid: VM ID
            disk_node: Block device node (e.g., "drive-scsi0")
            disk_driver: Disk format (qcow2, raw, etc.)
            bitmap_name: Name of the dirty bitmap
            bitmap_valid: Whether bitmap is currently valid
            last_full_backup: Timestamp of last full backup
            last_incremental_backup: Timestamp of last incremental
            backup_count: Number of incrementals since last full
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO vm_bitmap_state (
                    hypervisor_id, vmid, disk_node, disk_driver, bitmap_name,
                    bitmap_valid, last_full_backup, last_incremental_backup,
                    backup_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(hypervisor_id, vmid, disk_node) DO UPDATE SET
                    disk_driver = excluded.disk_driver,
                    bitmap_name = excluded.bitmap_name,
                    bitmap_valid = excluded.bitmap_valid,
                    last_full_backup = COALESCE(excluded.last_full_backup, last_full_backup),
                    last_incremental_backup = COALESCE(excluded.last_incremental_backup, last_incremental_backup),
                    backup_count = excluded.backup_count,
                    updated_at = excluded.updated_at
                """,
                (
                    hypervisor_id,
                    vmid,
                    disk_node,
                    disk_driver,
                    bitmap_name,
                    1 if bitmap_valid else 0,
                    last_full_backup,
                    last_incremental_backup,
                    backup_count,
                    now,
                    now,
                ),
            )

    def update_bitmap_after_backup(
        self,
        hypervisor_id: str,
        vmid: int,
        disk_node: str,
        backup_type: str,  # "full" or "incremental"
        timestamp: str | None = None,
    ) -> None:
        """Update bitmap state after a successful backup.

        Args:
            hypervisor_id: Hypervisor ID
            vmid: VM ID
            disk_node: Block device node
            backup_type: "full" or "incremental"
            timestamp: Backup timestamp (default: now)
        """
        now = timestamp or datetime.now().isoformat()
        with self._connect() as conn:
            if backup_type == "full":
                conn.execute(
                    """
                    UPDATE vm_bitmap_state
                    SET last_full_backup = ?,
                        backup_count = 0,
                        bitmap_valid = 1,
                        updated_at = ?
                    WHERE hypervisor_id = ? AND vmid = ? AND disk_node = ?
                    """,
                    (now, now, hypervisor_id, vmid, disk_node),
                )
            else:  # incremental
                conn.execute(
                    """
                    UPDATE vm_bitmap_state
                    SET last_incremental_backup = ?,
                        backup_count = backup_count + 1,
                        updated_at = ?
                    WHERE hypervisor_id = ? AND vmid = ? AND disk_node = ?
                    """,
                    (now, now, hypervisor_id, vmid, disk_node),
                )

    def invalidate_vm_bitmaps(
        self,
        hypervisor_id: str,
        vmid: int,
        disk_node: str | None = None,
    ) -> int:
        """Mark VM bitmaps as invalid (e.g., after VM reboot for RAW disks).

        Args:
            hypervisor_id: Hypervisor ID
            vmid: VM ID
            disk_node: Optional specific disk (None = all disks)

        Returns:
            Number of bitmaps invalidated
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            if disk_node:
                result = conn.execute(
                    """
                    UPDATE vm_bitmap_state
                    SET bitmap_valid = 0, updated_at = ?
                    WHERE hypervisor_id = ? AND vmid = ? AND disk_node = ?
                    """,
                    (now, hypervisor_id, vmid, disk_node),
                )
            else:
                result = conn.execute(
                    """
                    UPDATE vm_bitmap_state
                    SET bitmap_valid = 0, updated_at = ?
                    WHERE hypervisor_id = ? AND vmid = ?
                    """,
                    (now, hypervisor_id, vmid),
                )
            return result.rowcount

    def delete_vm_bitmap_state(
        self,
        hypervisor_id: str,
        vmid: int,
        disk_node: str | None = None,
    ) -> int:
        """Delete bitmap state records for a VM.

        Args:
            hypervisor_id: Hypervisor ID
            vmid: VM ID
            disk_node: Optional specific disk (None = all disks)

        Returns:
            Number of records deleted
        """
        with self._connect() as conn:
            if disk_node:
                result = conn.execute(
                    """
                    DELETE FROM vm_bitmap_state
                    WHERE hypervisor_id = ? AND vmid = ? AND disk_node = ?
                    """,
                    (hypervisor_id, vmid, disk_node),
                )
            else:
                result = conn.execute(
                    """
                    DELETE FROM vm_bitmap_state
                    WHERE hypervisor_id = ? AND vmid = ?
                    """,
                    (hypervisor_id, vmid),
                )
            return result.rowcount

    def can_do_incremental_backup(
        self,
        hypervisor_id: str,
        vmid: int,
    ) -> tuple[bool, str]:
        """Check if a VM can do incremental backup.

        A VM can do incremental if:
        - At least one disk has valid bitmap state
        - Has had at least one full backup

        Args:
            hypervisor_id: Hypervisor ID
            vmid: VM ID

        Returns:
            Tuple of (can_incremental, reason)
        """
        states = self.get_vm_bitmap_state(hypervisor_id, vmid)

        if not states:
            return False, "No bitmap tracking configured for this VM"

        valid_states = [s for s in states if s["bitmap_valid"]]
        if not valid_states:
            return False, "All bitmaps are invalid (may need full backup after VM restart)"

        has_full = any(s["last_full_backup"] for s in valid_states)
        if not has_full:
            return False, "No full backup exists yet"

        return True, "Incremental backup available"

    def _row_to_bitmap_state(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a database row to a bitmap state dict."""
        return {
            "id": row["id"],
            "hypervisor_id": row["hypervisor_id"],
            "vmid": row["vmid"],
            "disk_node": row["disk_node"],
            "disk_driver": row["disk_driver"],
            "bitmap_name": row["bitmap_name"],
            "bitmap_valid": bool(row["bitmap_valid"]),
            "last_full_backup": row["last_full_backup"],
            "last_incremental_backup": row["last_incremental_backup"],
            "backup_count": row["backup_count"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
