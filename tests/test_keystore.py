from pathlib import Path

from backer.core import keystore


def test_headless_fallback_round_trip_and_permissions(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(keystore.shutil, "which", lambda _: None)

    backend = keystore.put("backer/repo/home/passphrase", "secret")
    assert backend == ("dpapi" if keystore.os.name == "nt" else "file")
    assert keystore.get("backer/repo/home/passphrase") == "secret"
    if keystore.os.name != "nt":
        assert (tmp_path / "data" / "secrets").stat().st_mode & 0o777 == 0o700
        assert next((tmp_path / "data" / "secrets").iterdir()).stat().st_mode & 0o777 == 0o600
    keystore.delete("backer/repo/home/passphrase")
    assert keystore.get("backer/repo/home/passphrase") is None
