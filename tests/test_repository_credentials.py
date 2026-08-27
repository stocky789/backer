from pathlib import Path

from backer.server.storage import Storage


def test_repository_credentials_are_separate_and_legacy_job_password_is_encrypted(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "backer.db")
    storage.add_repository(
        "repo", "share", "smb", storage_password_encrypted=None,
        repository_password_encrypted=None,
    )
    storage.set_storage_password("repo", "smb-secret")
    storage.set_repository_password("repo", "repo-secret")

    assert storage.get_storage_password("repo") == "smb-secret"
    assert storage.get_repository_password("repo") == "repo-secret"

    storage.save_job("job", {"backend": "restic", "backend_options": {"restic_password": "old-secret"}})
    with storage._connect() as conn:
        raw = conn.execute("SELECT config FROM jobs WHERE name = 'job'").fetchone()["config"]
    assert "old-secret" not in raw
    assert storage.get_job("job")["backend_options"]["repository_password"] == "old-secret"
