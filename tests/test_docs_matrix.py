"""Published support matrix checks."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_readme_names_both_local_types() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Local directory (serverless, on this client)" in readme
    assert "Local directory (server-managed, via the proxy relay)" in readme


def test_readme_states_the_v1_serverless_matrix_and_limits() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "server-relay-only" in readme
    assert "| Repository | Serverless Linux | Serverless Windows | Server-managed mode only |" in readme
    assert "SMB" in readme and "S3" in readme
    assert "NFS" in readme
    assert "concurrent-writer" in readme
    assert "one designated maintenance owner" in readme
    assert "no cross-machine lease" in readme
