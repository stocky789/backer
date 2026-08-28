from pathlib import Path

from backer.server.storage import Storage


def test_repository_credentials_are_separate_and_encrypted(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "backer.db")
    storage.add_repository(
        "repo", "share", "smb", storage_password_encrypted=None,
        repository_password_encrypted=None,
    )
    storage.set_storage_password("repo", "smb-secret")
    storage.set_repository_password("repo", "repo-secret")

    assert storage.get_storage_password("repo") == "smb-secret"
    assert storage.get_repository_password("repo") == "repo-secret"
