import json
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

import pytest

from backer.server.secrets import get_secrets_manager
from backer.server.storage import Storage


def test_schema_upgrade_keeps_legacy_nfs_repository_password(tmp_path: Path) -> None:
    database = tmp_path / "backer.db"
    encrypted = get_secrets_manager(tmp_path).encrypt("repository-secret")
    with sqlite3.connect(database) as conn:
        conn.executescript("""
            CREATE TABLE repositories (
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
        """)
        conn.execute(
            "INSERT INTO repositories (id, name, repo_type, password_encrypted, created_at) VALUES (?, ?, ?, ?, ?)",
            ("nfs-repo", "NFS repository", "nfs", encrypted, "now"),
        )

    storage = Storage(database)

    assert storage.get_repository_password("nfs-repo") == "repository-secret"


def test_fresh_repository_schema_has_no_backend_type(tmp_path: Path) -> None:
    storage = Storage(tmp_path)

    with storage._connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(repositories)")}

    assert "backend_type" not in columns
    assert "repository_password_encrypted" in columns


@pytest.mark.parametrize(
    "read_jobs",
    [
        lambda storage, name: storage.get_job(name),
        lambda storage, _name: storage.list_jobs(),
    ],
)
def test_job_reads_do_not_migrate_legacy_configs(
    tmp_path: Path, read_jobs: Callable[[Storage, str], object]
) -> None:
    storage = Storage(tmp_path / "backer.db")
    storage.save_job("migrated", {"repository_id": "repo-1"})

    statements: list[str] = []
    original_connect = storage._connect

    @contextmanager
    def traced_connect():
        with original_connect() as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    storage._connect = traced_connect  # type: ignore[method-assign]
    read_jobs(storage, "migrated")
    assert not [statement for statement in statements if statement.startswith("UPDATE jobs")]

    with storage._connect() as conn:
        conn.execute(
            "INSERT INTO jobs (name, config, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (
                "legacy",
                json.dumps({"repository_id": "repo-1"}),
                "now",
                "now",
            ),
        )
    statements.clear()

    read_jobs(storage, "legacy")
    updates = [statement for statement in statements if statement.startswith("UPDATE jobs")]
    assert not updates
    with original_connect() as conn:
        config = conn.execute("SELECT config FROM jobs WHERE name = 'legacy'").fetchone()["config"]
    assert "repository_id" in config
