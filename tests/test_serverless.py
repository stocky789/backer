from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from click.testing import CliRunner

from backer.backends.base import BackupDestination
from backer.backends.kopia import KopiaBackend
from backer.cli import main
from backer.core.config import BackerConfig, JobConfig, RepositoryConfig, RetentionConfig, SourceConfig
from backer.core.job import JobRun, JobStatus


def test_probe_distinguishes_absent_unreachable_and_wrong_passphrase(monkeypatch) -> None:
    backend = KopiaBackend({"repository_password": "correct"})
    monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
    messages = [
        "repository not initialized in the provided storage",
        "cannot access storage path",
        "invalid repository password",
    ]

    def run(command, **_):
        if command[1:3] == ["repository", "disconnect"]:
            return CompletedProcess(command, 0, "", "")
        return CompletedProcess(command, 1, "", messages.pop(0))

    monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
    assert backend.repository_probe("missing")[0] == "absent"
    assert backend.repository_probe("offline")[0] == "unreachable"
    assert backend.repository_probe("wrong")[0] == "wrong_passphrase"


def test_serverless_connect_resets_connection_and_never_persists_credentials(monkeypatch) -> None:
    backend = KopiaBackend({"repository_password": "secret"})
    calls = []
    monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))

    def run(command, **_):
        calls.append(command)
        if command[1:3] == ["repository", "status"]:
            return CompletedProcess(command, 0, '{"uniqueIDHex":"abc"}', "")
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
    assert backend.repository_probe("repo") == ("present", "abc")
    assert calls[0][1:3] == ["repository", "disconnect"]
    assert "--no-persist-credentials" in calls[1]
    assert all("--use-credential-manager" not in call for call in calls)


def test_init_resets_and_disconnects_even_after_create_failure(monkeypatch, tmp_path: Path) -> None:
    backend = KopiaBackend({"repository_password": "secret"})
    calls = []
    monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))

    def run(command, **_):
        calls.append(command)
        if command[1:3] == ["repository", "create"]:
            return CompletedProcess(command, 1, "", "create failed")
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
    result = backend.init_repo(BackupDestination(str(tmp_path / "repo")))

    assert not result.success
    assert calls[0][1:3] == ["repository", "disconnect"]
    assert "--no-persist-credentials" in calls[1]
    assert calls[-1][1:3] == ["repository", "disconnect"]


@pytest.mark.parametrize("failure_at", ["connect", "status"])
def test_probe_preserves_kopia_error_text(monkeypatch, failure_at: str) -> None:
    backend = KopiaBackend({"repository_password": "secret"})
    monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))

    def run(command, **_):
        if command[1:3] == ["repository", "disconnect"]:
            return CompletedProcess(command, 0, "", "")
        if failure_at == "connect" or command[1:3] == ["repository", "status"]:
            return CompletedProcess(command, 1, "", "cannot access storage path: offline\n")
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)

    assert backend.repository_probe("offline")[0] == "unreachable"
    assert backend.last_repository_error == "cannot access storage path: offline\n"


def test_repo_attach_refuses_absent_without_create(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("backer.serverless.repositories.probe", lambda *_: ("absent", None, "nothing there"))
    created = []
    monkeypatch.setattr("backer.serverless.repositories.create", lambda *_: created.append(True))

    result = CliRunner().invoke(main, [
        "repo", "add", "Home", "--attach", "--path", str(tmp_path / "repo"), "--passphrase-stdin"
    ], input="secret\n")

    assert result.exit_code != 0
    assert created == []
    assert "nothing" in result.output.lower()


def test_repo_init_stores_only_verified_passphrase(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path / "data"))
    states = iter([("absent", None, ""), ("present", "unique", "")])
    monkeypatch.setattr("backer.serverless.repositories.probe", lambda *_: next(states))
    monkeypatch.setattr("backer.serverless.repositories.create", lambda *_: (True, ""))
    monkeypatch.setattr("backer.serverless.repositories.keystore.put", lambda *args, **_: "file")

    result = CliRunner().invoke(main, [
        "repo", "add", "Home", "--init", "--path", str(tmp_path / "repo"), "--passphrase-stdin"
    ], input="secret\n")

    assert result.exit_code == 0, result.output
    saved = (tmp_path / "config.yaml").read_text()
    assert "secret" not in saved
    assert "unique_id: unique" in saved


def test_repo_add_requires_headless_for_file_keystore(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("backer.serverless.repositories.file_fallback_required", lambda: True)

    result = CliRunner().invoke(main, [
        "repo", "add", "Home", "--attach", "--path", "repo", "--passphrase-stdin"
    ], input="secret\n")

    assert result.exit_code != 0
    assert "--headless" in result.output


def test_repository_config_keeps_s3_keys_out_of_config() -> None:
    from backer.core.config import RepositoryConfig

    record = RepositoryConfig(
        id="repo", name="Repo", type="s3", bucket="bucket", prefix="", endpoint="https://s3.example", region="us-east-1"
    )
    assert "access_key_id" not in record.model_dump(exclude_none=True)


def test_preflight_failure_writes_one_local_attempt(monkeypatch, tmp_path: Path) -> None:
    from backer.serverless.runs import run_local_job
    from backer.serverless.store import read_runs

    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path))
    config = BackerConfig(
        agent_id="agent-one",
        repositories={"repo": RepositoryConfig(name="Repo", type="local", path="repo")},
        jobs={"nightly": JobConfig(repository="repo", source=SourceConfig(path="/data"))},
    )
    report = run_local_job(config, "nightly")
    attempts = read_runs(tmp_path, "nightly", 2)

    assert not report["success"]
    assert len(attempts) == 1
    assert attempts[0].error_stage == "keystore"
    assert (tmp_path / "last_attempt" / "nightly.json").exists()


def test_preview_and_apply_differ_only_by_delete(monkeypatch, tmp_path: Path) -> None:
    from backer.serverless.retention import prune_job

    config = BackerConfig(
        repositories={"repo": RepositoryConfig(name="Repo", type="local", path="repo", passphrase_ref="pass")},
        jobs={
            "nightly": JobConfig(
                repository="repo", source=SourceConfig(path="/data"), retention=RetentionConfig(keep_last=1)
            )
        },
    )
    monkeypatch.setattr("backer.serverless.retention.keystore.get", lambda *_args, **_kwargs: "passphrase")
    calls: list[bool] = []

    class Backend:
        def prune(self, *_args, **kwargs):
            calls.append(kwargs["dry_run"])
            return type("Result", (), {"success": True, "output": "2 snapshot(s) of source would be deleted"})()

    monkeypatch.setattr("backer.serverless.retention._backend", lambda *_: Backend())
    assert prune_job(config, "nightly") == (2, "2 snapshot(s) of source would be deleted")
    prune_job(config, "nightly", apply=True)
    assert calls == [True, False]


def test_local_job_create_refuses_duplicate_source(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    config = BackerConfig(repositories={"repo": RepositoryConfig(name="Repo", type="local", path="repo")},
                          jobs={"first": JobConfig(repository="repo", source=SourceConfig(path="/data"))})
    config.save(tmp_path / "config.yaml")

    result = CliRunner().invoke(
        main, ["job", "create", "--name", "second", "--source", "/data", "--repository", "repo"]
    )

    assert result.exit_code != 0
    assert "first" in result.output


def test_local_attempt_is_atomic_and_utc(tmp_path: Path, monkeypatch) -> None:
    from backer.serverless.store import append_run, read_runs

    replaces: list[tuple[Path, Path]] = []
    original = __import__("os").replace
    def replace(source: str, target: str) -> None:
        replaces.append((Path(source), Path(target)))
        original(source, target)

    monkeypatch.setattr("backer.serverless.store.os.replace", replace)
    append_run(tmp_path, JobRun("nightly", "20260901T020000Z-agent", JobStatus.FAILED,
                               datetime(2026, 9, 1, 2, tzinfo=UTC), datetime(2026, 9, 1, 2, 1, tzinfo=UTC)))

    attempt = read_runs(tmp_path, "nightly", 1)[0]
    assert attempt.to_dict()["started_at"].endswith("Z")
    assert len(replaces) == 2
    assert all(source.parent == target.parent and source.suffix == ".tmp" for source, target in replaces)


def test_progress_is_local_and_removed_after_preflight_failure(monkeypatch, tmp_path: Path) -> None:
    from backer.serverless.runs import run_local_job

    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path))
    config = BackerConfig(
        repositories={"repo": RepositoryConfig(name="Repo", type="local", path="repo")},
        jobs={"nightly": JobConfig(repository="repo", source=SourceConfig(path="/data"))},
    )

    assert not run_local_job(config, "nightly")["success"]
    assert not list((tmp_path / "progress").glob("*.json"))
    assert list((tmp_path / "logs").glob("*.log"))


def test_stale_cutoff_counts_two_complete_daily_intervals() -> None:
    from backer.cli import _stale_cutoff

    now = datetime(2026, 9, 1, 15, tzinfo=UTC)

    assert _stale_cutoff("0 2 * * *", now) == datetime(2026, 8, 30, 15, tzinfo=UTC)


def test_stale_cutoff_uses_uneven_cron_intervals() -> None:
    from backer.cli import _stale_cutoff

    now = datetime(2026, 3, 20, tzinfo=UTC)

    assert _stale_cutoff("0 0 1,15 * *", now) == datetime(2026, 2, 17, tzinfo=UTC)
