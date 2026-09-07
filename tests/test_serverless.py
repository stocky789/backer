import os
import sys
from contextlib import contextmanager
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


def _mock_smb_session(
    monkeypatch,
    order: list[str],
    *,
    mount_point: Path | None = None,
    serverless_session_created: bool = True,
) -> None:
    if sys.platform == "win32":

        class Manager:
            serverless_session_created = serverless_session_created

            def connect_serverless(self, *_args, **_kwargs) -> bool:
                order.append("connect")
                return True

            def disconnect_serverless(self, *_args) -> None:
                order.append("disconnect")

        monkeypatch.setattr("backer.core.mounts.SMBConnectionManager", Manager)
    else:

        @contextmanager
        def smb_mount(*_args):
            order.append("connect")
            try:
                yield mount_point or Path("/tmp/mounted")
            finally:
                order.append("disconnect")

        monkeypatch.setattr("backer.core.mounts.smb_mount_context", smb_mount)


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


def test_serverless_connect_resets_connection_and_never_persists_credentials(monkeypatch, tmp_path: Path) -> None:
    backend = KopiaBackend({"repository_password": "secret"})
    calls = []
    monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))
    monkeypatch.setattr(
        backend,
        "_repo_env",
        lambda _path: {"KOPIA_CACHE_DIRECTORY": str(tmp_path)},
    )

    def run(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["repository", "connect"]:
            (tmp_path / "kopia.repository").write_text('{"uniqueID":"q80="}')
            assert kwargs["timeout"] == 300
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
    assert backend.repository_probe("repo") == ("present", "abcd")
    assert calls[0][1:3] == ["repository", "disconnect"]
    assert "--no-persist-credentials" in calls[1]
    assert all(call[1:3] != ["repository", "status"] for call in calls)
    assert all("--use-credential-manager" not in call for call in calls)


def test_probe_never_trusts_a_stale_cached_repository_id(monkeypatch, tmp_path: Path) -> None:
    backend = KopiaBackend({"repository_password": "secret"})
    cache = tmp_path / "kopia.repository"
    cache.write_text('{"uniqueID":"q80="}', encoding="utf-8")
    monkeypatch.setattr(backend, "_repo_env", lambda _path: {"KOPIA_CACHE_DIRECTORY": str(tmp_path)})
    monkeypatch.setattr(backend, "_connect_repo", lambda _path: (True, "Connected"))
    monkeypatch.setattr(backend, "_disconnect_repo", lambda _path: None)

    assert backend.repository_probe("repo")[0] == "unreachable"
    assert not cache.exists()


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


def test_probe_preserves_kopia_error_text(monkeypatch) -> None:
    backend = KopiaBackend({"repository_password": "secret"})
    monkeypatch.setattr(backend, "_get_binary", lambda: Path("kopia"))

    def run(command, **_):
        if command[1:3] == ["repository", "disconnect"]:
            return CompletedProcess(command, 0, "", "")
        return CompletedProcess(command, 1, "", "cannot access storage path: offline\n")

    monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)

    assert backend.repository_probe("offline")[0] == "unreachable"
    assert backend.last_repository_error == "cannot access storage path: offline\n"


def test_repo_attach_refuses_absent_without_create(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("backer.serverless.repositories.probe", lambda *_: ("absent", None, "nothing there"))
    created = []
    monkeypatch.setattr("backer.serverless.repositories.create", lambda *_: created.append(True))

    result = CliRunner().invoke(
        main,
        ["repo", "add", "Home", "--attach", "--path", str(tmp_path / "repo"), "--passphrase-stdin", "--headless"],
        input="secret\n",
    )

    assert result.exit_code != 0
    assert created == []
    assert "nothing" in result.output.lower()


def test_repo_init_onto_existing_repository_says_it_already_exists(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("backer.serverless.repositories.probe", lambda *_: ("present", "abc", ""))
    (tmp_path / "repo").mkdir()

    result = CliRunner().invoke(
        main,
        ["repo", "add", "Home", "--init", "--path", str(tmp_path / "repo"), "--passphrase-stdin", "--headless"],
        input="secret\n",
    )

    assert result.exit_code != 0
    assert "already exists at this location" in result.output


def test_repo_init_onto_existing_repository_with_wrong_passphrase_is_explained(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        "backer.serverless.repositories.probe",
        lambda *_: ("wrong_passphrase", None, "invalid repository password"),
    )
    (tmp_path / "repo").mkdir()

    result = CliRunner().invoke(
        main,
        ["repo", "add", "Home", "--init", "--path", str(tmp_path / "repo"), "--passphrase-stdin", "--headless"],
        input="wrong\n",
    )

    assert result.exit_code != 0
    assert "passphrase entered does not open it" in result.output
    assert "invalid repository password" not in result.output


def test_repo_attach_with_wrong_passphrase_is_explained(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        "backer.serverless.repositories.probe",
        lambda *_: ("wrong_passphrase", None, "invalid repository password"),
    )
    (tmp_path / "repo").mkdir()

    result = CliRunner().invoke(
        main,
        ["repo", "add", "Home", "--attach", "--path", str(tmp_path / "repo"), "--passphrase-stdin", "--headless"],
        input="wrong\n",
    )

    assert result.exit_code != 0
    assert "passphrase entered does not open it" in result.output


def test_repo_init_stores_only_verified_passphrase(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path / "data"))
    states = iter([("absent", None, ""), ("present", "unique", "")])
    monkeypatch.setattr("backer.serverless.repositories.probe", lambda *_: next(states))
    monkeypatch.setattr("backer.serverless.repositories.create", lambda *_: (True, ""))
    monkeypatch.setattr("backer.serverless.repositories.set_maintenance_owner", lambda *_: (True, ""))
    monkeypatch.setattr("backer.serverless.repositories.keystore.put", lambda *args, **_: "file")

    result = CliRunner().invoke(
        main,
        ["repo", "add", "Home", "--init", "--path", str(tmp_path / "repo"), "--passphrase-stdin", "--headless"],
        input="secret\n",
    )

    assert result.exit_code == 0, result.output
    saved = (tmp_path / "config.yaml").read_text()
    assert "secret" not in saved
    assert "unique_id: unique" in saved


def test_repo_init_creates_the_missing_repository_folder(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path / "data"))
    states = iter([("absent", None, ""), ("present", "unique", "")])
    monkeypatch.setattr("backer.serverless.repositories.probe", lambda *_: next(states))
    monkeypatch.setattr("backer.serverless.repositories.create", lambda *_: (True, ""))
    monkeypatch.setattr("backer.serverless.repositories.set_maintenance_owner", lambda *_: (True, ""))
    monkeypatch.setattr("backer.serverless.repositories.keystore.put", lambda *args, **_: "file")
    target = tmp_path / "share" / "nested" / "repo"
    (tmp_path / "share").mkdir()

    result = CliRunner().invoke(
        main,
        ["repo", "add", "Home", "--init", "--path", str(target), "--passphrase-stdin", "--headless"],
        input="secret\n",
    )

    assert result.exit_code == 0, result.output
    assert target.is_dir()


def test_repo_init_reports_a_denied_repository_folder_plainly(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path / "data"))

    def deny(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("backer.serverless.repositories.os.makedirs", deny)

    result = CliRunner().invoke(
        main,
        ["repo", "add", "Home", "--init", "--path", str(tmp_path / "repo"), "--passphrase-stdin", "--headless"],
        input="secret\n",
    )

    assert result.exit_code != 0
    assert "denied creating the repository folder" in result.output
    assert "write access" in result.output


def test_repo_attach_never_creates_the_missing_folder(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("backer.serverless.repositories.probe", lambda *_: ("unreachable", None, "cannot access"))
    target = tmp_path / "missing" / "repo"

    result = CliRunner().invoke(
        main,
        ["repo", "add", "Home", "--attach", "--path", str(target), "--passphrase-stdin", "--headless"],
        input="secret\n",
    )

    assert result.exit_code != 0
    assert not target.exists()
    assert not target.parent.exists()


def test_repo_add_requires_headless_for_file_keystore(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("backer.serverless.repositories.file_fallback_required", lambda: True)

    result = CliRunner().invoke(
        main, ["repo", "add", "Home", "--attach", "--path", "repo", "--passphrase-stdin"], input="secret\n"
    )

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


def test_cancelled_preflight_records_one_cancelled_attempt(monkeypatch, tmp_path: Path) -> None:
    import threading

    from backer.serverless.runs import run_local_job
    from backer.serverless.store import read_runs

    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path))
    cancelled = threading.Event()
    cancelled.set()
    config = BackerConfig(
        agent_id="agent-one",
        jobs={"nightly": JobConfig(repository="repo", source=SourceConfig(path="/data"))},
    )

    report = run_local_job(config, "nightly", cancel_event=cancelled)
    attempts = read_runs(tmp_path, "nightly", 2)

    assert report["cancelled"] and len(attempts) == 1 and attempts[0].status.value == "cancelled"


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
            output = (
                "2 snapshot(s) of source would be deleted"
                if kwargs["dry_run"]
                else "Deleted 2 snapshots of source"
            )
            return type("Result", (), {"success": True, "output": output})()

    monkeypatch.setattr("backer.serverless.retention._backend", lambda *_: Backend())
    preview_count, preview, snapshots = prune_job(config, "nightly")
    assert preview_count == 2
    assert preview == "2 snapshot(s) of source would be deleted"
    assert snapshots == []
    assert prune_job(config, "nightly", apply=True) == (2, "Deleted 2 snapshots of source", [])
    assert calls == [True, False]


def test_prune_loads_smb_storage_password_before_opening_operation(monkeypatch) -> None:
    from backer.serverless.retention import prune_job

    config = BackerConfig(
        repositories={
            "repo": RepositoryConfig(
                name="NAS", type="smb", server="nas", share="backups", username="matt",
                passphrase_ref="pass", storage_password_ref="smb",
            )
        },
        jobs={
            "nightly": JobConfig(
                repository="repo", source=SourceConfig(path="/data"), retention=RetentionConfig(keep_last=1)
            )
        },
    )
    monkeypatch.setattr(
        "backer.serverless.retention.keystore.get",
        lambda reference, **_kwargs: {"pass": "passphrase", "smb": "smb-password"}[reference],
    )
    storage: list[str | None] = []

    @contextmanager
    def operation_context(record, value):
        storage.append(value)
        yield record

    class Backend:
        def prune(self, *_args, **_kwargs):
            return type("Result", (), {"success": True, "output": "Nothing to delete"})()

    monkeypatch.setattr("backer.serverless.retention.repository_operation_context", operation_context)
    monkeypatch.setattr("backer.serverless.retention._backend", lambda *_: Backend())

    prune_job(config, "nightly")

    assert storage == ["smb-password"]


def test_local_job_create_refuses_duplicate_source(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    config = BackerConfig(
        repositories={"repo": RepositoryConfig(name="Repo", type="local", path="repo")},
        jobs={"first": JobConfig(repository="repo", source=SourceConfig(path="/data"))},
    )
    config.save(tmp_path / "config.yaml")

    result = CliRunner().invoke(
        main, ["job", "create", "--name", "second", "--source", "/data", "--repository", "repo", "--no-schedule"]
    )

    assert result.exit_code != 0
    assert "Source '/data' is already owned by job 'first'" in result.output


def test_local_attempt_is_atomic_and_utc(tmp_path: Path, monkeypatch) -> None:
    from backer.serverless.store import append_run, read_runs

    replaces: list[tuple[Path, Path]] = []
    original = __import__("os").replace

    def replace(source: str, target: str) -> None:
        replaces.append((Path(source), Path(target)))
        original(source, target)

    monkeypatch.setattr("backer.serverless.store.os.replace", replace)
    append_run(
        tmp_path,
        JobRun(
            "nightly",
            "20260901T020000Z-agent",
            JobStatus.FAILED,
            datetime(2026, 9, 1, 2, tzinfo=UTC),
            datetime(2026, 9, 1, 2, 1, tzinfo=UTC),
        ),
    )

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


def test_smb_preflight_authenticates_before_probe_and_records_failure(monkeypatch, tmp_path: Path) -> None:
    """Removing the serverless SMB session must make the UNC probe fail safely."""
    from backer.serverless.runs import run_local_job
    from backer.serverless.store import read_runs

    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path))
    order: list[str] = []
    _mock_smb_session(monkeypatch, order, mount_point=tmp_path / "mounted")
    monkeypatch.setattr("backer.serverless.runs.keystore.get", lambda key, **_: "pass" if key == "pass" else "smb")

    def offline(*_args):
        assert order == ["connect"]
        return "unreachable", None, "share unavailable"

    monkeypatch.setattr("backer.serverless.runs.probe", offline)
    config = BackerConfig(
        repositories={
            "repo": RepositoryConfig(
                name="Repo",
                type="smb",
                server="nas",
                share="backups",
                username="backup",
                passphrase_ref="pass",
                storage_password_ref="storage",
            )
        },
        jobs={"nightly": JobConfig(repository="repo", source=SourceConfig(path="/data"))},
    )

    assert not run_local_job(config, "nightly")["success"]
    assert order == ["connect", "disconnect"]
    assert read_runs(tmp_path, "nightly", 1)[0].error_stage == "prepare_destination"


def test_explicit_and_due_runs_share_one_nonblocking_lock(monkeypatch, tmp_path: Path) -> None:
    """Removing the explicit lock boundary would allow overlapping local backups."""
    from backer.serverless.runs import run_local_job
    from backer.serverless.schedule import run_lock

    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path))
    config = BackerConfig(jobs={"nightly": JobConfig(repository="repo", source=SourceConfig(path="/data"))})

    with run_lock(tmp_path):
        assert run_local_job(config, "nightly") is None


def test_job_run_lock_holder_fails_explicitly_but_skips_due(monkeypatch, tmp_path: Path) -> None:
    """Returning a lock-holder result as a failed attempt would violate scheduler safety."""
    config = BackerConfig(jobs={"nightly": JobConfig(repository="repo", source=SourceConfig(path="/data"))})
    config.save(tmp_path / "config.yaml")
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("backer.serverless.runs.run_local_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("backer.serverless.runs.run_due_jobs", lambda *_args, **_kwargs: None)

    explicit = CliRunner().invoke(main, ["job", "run", "nightly"])
    due = CliRunner().invoke(main, ["job", "run", "--due"])

    assert explicit.exit_code != 0
    assert "another local backup" in explicit.output.lower()
    assert due.exit_code == 0
    assert due.output.strip() == "Another local backup is running"


def test_repo_add_s3_stores_credentials_outside_config_and_adopt_never_creates(monkeypatch, tmp_path: Path) -> None:
    """Dropping typed S3 input or treating adopt as init would leak or create data."""
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("backer.serverless.repositories.probe", lambda *_: ("present", "id", ""))
    puts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "backer.serverless.repositories.keystore.put", lambda key, value, **_: puts.append((key, value)) or "file"
    )
    created: list[bool] = []
    monkeypatch.setattr("backer.serverless.repositories.create", lambda *_: created.append(True))
    passphrase_file = tmp_path / "passphrase"
    passphrase_file.write_text("repo-pass")

    result = CliRunner().invoke(
        main,
        [
            "repo",
            "add",
            "Cloud",
            "--init",
            "--adopt",
            "--type",
            "s3",
            "--bucket",
            "bucket",
            "--endpoint",
            "https://s3.example",
            "--region",
            "us-east-1",
            "--storage-stdin",
            "--passphrase-file",
            str(passphrase_file),
            "--headless",
        ],
        input='{"access_key_id":"access","secret_access_key":"storage-secret"}\n',
    )

    assert result.exit_code == 0, result.output
    assert created == []
    assert any("storage-secret" in value for _, value in puts)
    assert "repo-pass" not in (tmp_path / "config.yaml").read_text()
    assert "storage-secret" not in (tmp_path / "config.yaml").read_text()


@pytest.mark.parametrize(
    ("attach", "init", "states", "expected"),
    [
        (True, False, [("present", "id", "")], ["connect", "probe", "disconnect"]),
        (
            False,
            True,
            [("absent", None, ""), ("present", "id", "")],
            ["connect", "probe", "create", "probe", "owner", "disconnect"],
        ),
    ],
)
def test_smb_repository_add_authenticates_before_every_probe(
    monkeypatch, tmp_path: Path, attach, init, states, expected
) -> None:
    """Removing the setup-time SMB session would probe or create a UNC unauthenticated."""
    from backer.serverless.repositories import add_repository

    order: list[str] = []
    _mock_smb_session(monkeypatch, order)
    monkeypatch.setattr("backer.serverless.repositories.file_fallback_required", lambda: False)
    monkeypatch.setattr("backer.serverless.repositories.keystore.put", lambda *_args, **_kwargs: "file")

    def checked_probe(*_args):
        assert order and order[0] == "connect"
        order.append("probe")
        return states.pop(0)

    monkeypatch.setattr("backer.serverless.repositories.probe", checked_probe)
    monkeypatch.setattr("backer.serverless.repositories.create", lambda *_args: order.append("create") or (True, ""))
    monkeypatch.setattr(
        "backer.serverless.repositories.set_maintenance_owner", lambda *_args: order.append("owner") or (True, "")
    )
    record = RepositoryConfig(name="NAS", type="smb", server="nas", share="backups", username="backup", path="backer")

    add_repository(
        BackerConfig(),
        tmp_path / "config.yaml",
        "NAS",
        record,
        "repo-pass",
        attach=attach,
        init=init,
        storage="smb-password",
        headless=True,
    )

    assert order == expected


@pytest.mark.parametrize("repo_type", ["local", "smb", "s3"])
def test_new_repository_sets_maintenance_owner_before_persisting_secrets(
    monkeypatch, tmp_path: Path, repo_type: str
) -> None:
    from backer.serverless.repositories import add_repository

    monkeypatch.setattr("backer.serverless.repositories.file_fallback_required", lambda: False)
    calls: list[str] = []
    states = [("absent", None, ""), ("present", "id", "")]
    monkeypatch.setattr("backer.serverless.repositories.probe", lambda *_: states.pop(0))
    monkeypatch.setattr("backer.serverless.repositories.create", lambda *_: (True, ""))
    monkeypatch.setattr(
        "backer.serverless.repositories.set_maintenance_owner", lambda *_: calls.append("owner") or (True, "")
    )
    monkeypatch.setattr(
        "backer.serverless.repositories.keystore.put", lambda *_args, **_kwargs: calls.append("secret") or "file"
    )
    record = RepositoryConfig(
        name="Repo",
        type=repo_type,
        path="repo",
        server="nas" if repo_type == "smb" else None,
        share="share" if repo_type == "smb" else None,
        username="user" if repo_type == "smb" else None,
        bucket="bucket" if repo_type == "s3" else None,
        endpoint="https://s3.example" if repo_type == "s3" else None,
        region="us-east-1" if repo_type == "s3" else None,
    )
    if repo_type == "smb":
        _mock_smb_session(monkeypatch, [])

    add_repository(
        BackerConfig(agent_id="agent"),
        tmp_path / "config.yaml",
        "Repo",
        record,
        "pass",
        attach=False,
        init=True,
        storage={"access_key_id": "key", "secret_access_key": "secret"}
        if repo_type == "s3"
        else "smb"
        if repo_type == "smb"
        else None,
        headless=True,
    )

    assert calls[0] == "owner"
    assert calls.count("owner") == 1


@pytest.mark.parametrize("attach,adopt", [(True, False), (False, True)])
def test_attach_and_adopt_never_change_maintenance_owner(
    monkeypatch, tmp_path: Path, attach: bool, adopt: bool
) -> None:
    from backer.serverless.repositories import add_repository

    monkeypatch.setattr("backer.serverless.repositories.file_fallback_required", lambda: False)
    monkeypatch.setattr("backer.serverless.repositories.probe", lambda *_: ("present", "id", ""))
    monkeypatch.setattr("backer.serverless.repositories.keystore.put", lambda *_args, **_kwargs: "file")
    monkeypatch.setattr("backer.serverless.repositories.set_maintenance_owner", lambda *_: pytest.fail("owner changed"))

    add_repository(
        BackerConfig(),
        tmp_path / "config.yaml",
        "Repo",
        RepositoryConfig(name="Repo", type="local", path="repo"),
        "pass",
        attach=attach,
        init=not attach,
        adopt=adopt,
        headless=True,
    )


def test_smb_repository_add_disconnects_after_probe_failure(monkeypatch, tmp_path: Path) -> None:
    """An SMB setup failure must not leave this process's temporary session behind."""
    from backer.serverless.repositories import add_repository

    order: list[str] = []
    _mock_smb_session(monkeypatch, order)
    monkeypatch.setattr("backer.serverless.repositories.file_fallback_required", lambda: False)
    monkeypatch.setattr(
        "backer.serverless.repositories.probe", lambda *_args: order.append("probe") or ("unreachable", None, "offline")
    )
    record = RepositoryConfig(name="NAS", type="smb", server="nas", share="backups", username="backup")

    with pytest.raises(ValueError, match="offline"):
        add_repository(
            BackerConfig(),
            tmp_path / "config.yaml",
            "NAS",
            record,
            "repo-pass",
            attach=True,
            init=False,
            storage="smb-password",
            headless=True,
        )

    assert order == ["connect", "probe", "disconnect"]


def test_smb_attach_reuses_a_verified_windows_connection_without_tearing_it_down(monkeypatch, tmp_path: Path) -> None:
    from backer.core.config import BackerConfig, RepositoryConfig
    from backer.serverless import repositories

    monkeypatch.setattr(repositories.sys, "platform", "win32", raising=False)

    order = []

    class Manager:
        serverless_session_created = False

        def connect_existing_serverless(self, *_args) -> bool:
            order.append("reuse")
            return True

        def disconnect_serverless(self, *_args) -> None:
            order.append("disconnect")

    monkeypatch.setattr("backer.core.mounts.SMBConnectionManager", Manager)
    monkeypatch.setattr("backer.serverless.repositories.file_fallback_required", lambda: False)
    monkeypatch.setattr(
        "backer.serverless.repositories.probe", lambda *_args: order.append("probe") or ("present", "id", "")
    )
    monkeypatch.setattr("backer.serverless.repositories.keystore.put", lambda *_args, **_kwargs: "test")
    record = RepositoryConfig(
        name="NAS", type="smb", server="nas", share="backups", username="backup", use_existing_session=True
    )

    config = BackerConfig()
    repository_id, _ = repositories.add_repository(
        config,
        tmp_path / "config.yaml",
        "NAS",
        record,
        "repo-pass",
        attach=True,
        init=False,
        storage=None,
        headless=True,
    )

    assert order == ["reuse", "probe"]
    assert config.repositories[repository_id].storage_password_ref is None


def test_linux_smb_repository_setup_mounts_once_for_probe_create_and_owner(monkeypatch, tmp_path: Path) -> None:
    from backer.serverless import repositories

    monkeypatch.setattr(repositories.sys, "platform", "linux", raising=False)
    monkeypatch.setattr(repositories, "file_fallback_required", lambda: False)
    monkeypatch.setattr(
        repositories, "SMBConnectionManager", lambda: pytest.fail("Windows net use invoked"), raising=False
    )
    order: list[object] = []

    @contextmanager
    def mount(*_args):
        order.append("mount")
        yield tmp_path / "mounted"
        order.append("unmount")

    monkeypatch.setattr("backer.core.mounts.smb_mount_context", mount)
    states = [("absent", None, ""), ("present", "id", "")]
    monkeypatch.setattr(
        repositories, "probe", lambda record, *_: order.append(("probe", record.type, record.path)) or states.pop(0)
    )
    monkeypatch.setattr(
        repositories, "create", lambda record, *_: order.append(("create", record.type, record.path)) or (True, "")
    )
    monkeypatch.setattr(
        repositories,
        "set_maintenance_owner",
        lambda record, *_: order.append(("owner", record.type, record.path)) or (True, ""),
    )
    monkeypatch.setattr(repositories.keystore, "put", lambda *_args, **_kwargs: "file")

    repositories.add_repository(
        BackerConfig(agent_id="agent"),
        tmp_path / "config.yaml",
        "NAS",
        RepositoryConfig(name="NAS", type="smb", server="nas", share="share", username="user", path="sub/dir"),
        "pass",
        attach=False,
        init=True,
        storage="smb-pass",
        headless=True,
    )

    assert order == [
        "mount",
        ("probe", "local", str(tmp_path / "mounted" / "sub" / "dir")),
        ("create", "local", str(tmp_path / "mounted" / "sub" / "dir")),
        ("probe", "local", str(tmp_path / "mounted" / "sub" / "dir")),
        ("owner", "local", str(tmp_path / "mounted" / "sub" / "dir")),
        "unmount",
    ]


def test_linux_smb_run_mounts_once_for_probe_and_backup(monkeypatch, tmp_path: Path) -> None:
    from backer.serverless import runs

    monkeypatch.setattr(runs.sys, "platform", "linux", raising=False)
    monkeypatch.setattr(runs, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(runs, "SMBConnectionManager", lambda: pytest.fail("Windows net use invoked"), raising=False)
    order: list[object] = []

    @contextmanager
    def mount(*_args):
        order.append("mount")
        yield tmp_path / "mounted"
        order.append("unmount")

    monkeypatch.setattr("backer.core.mounts.smb_mount_context", mount)
    monkeypatch.setattr(runs.keystore, "get", lambda ref, **_: "pass" if ref == "pass" else "smb-pass")
    monkeypatch.setattr(
        runs, "probe", lambda record, *_: order.append(("probe", record.type, record.path)) or ("present", "id", "")
    )
    monkeypatch.setattr(
        runs,
        "run_backup",
        lambda job, **_: order.append(("run", job["destination_path"])) or {"success": True, "output": "ok"},
    )
    config = BackerConfig(
        repositories={
            "repo": RepositoryConfig(
                name="NAS",
                type="smb",
                server="nas",
                share="share",
                username="user",
                path="sub/dir",
                passphrase_ref="pass",
                storage_password_ref="smb",
            )
        },
        jobs={"nightly": JobConfig(repository="repo", source=SourceConfig(path=str(tmp_path / "source")))},
    )

    assert runs._run_local_job(config, "nightly")["success"]
    assert order == [
        "mount",
        ("probe", "local", str(tmp_path / "mounted" / "sub" / "dir")),
        ("run", str(tmp_path / "mounted" / "sub" / "dir")),
        "unmount",
    ]


@pytest.mark.parametrize("path", ["../outside", "/absolute", r"C:\\outside"])
def test_linux_smb_repository_context_rejects_escaping_subpaths(monkeypatch, path: str) -> None:
    from backer.serverless import repositories

    monkeypatch.setattr(repositories.sys, "platform", "linux")
    record = RepositoryConfig(name="NAS", type="smb", server="nas", share="share", username="user", path=path)

    with pytest.raises(ValueError, match="relative"):
        with repositories.repository_operation_context(record, "smb-pass"):
            pytest.fail("unsafe SMB subpath mounted")


def test_linux_smb_repository_context_requires_a_password(monkeypatch) -> None:
    from backer.serverless import repositories

    monkeypatch.setattr(repositories.sys, "platform", "linux")
    record = RepositoryConfig(name="NAS", type="smb", server="nas", share="share", username="user")

    with pytest.raises(ValueError, match="password"):
        with repositories.repository_operation_context(record, None):
            pytest.fail("passwordless SMB mount")


@pytest.mark.parametrize("created, expected", [(True, ["connect", "disconnect"]), (False, ["connect"])])
def test_windows_smb_repository_context_authenticates_before_yield_and_only_disconnects_owned_session(
    monkeypatch, created: bool, expected: list[str]
) -> None:
    from backer.serverless import repositories

    monkeypatch.setattr(repositories.sys, "platform", "win32")
    order: list[str] = []

    class Manager:
        serverless_session_created = created

        def connect_serverless(self, *_args, **_kwargs):
            order.append("connect")
            return True

        def disconnect_serverless(self, *_args):
            order.append("disconnect")

    monkeypatch.setattr("backer.core.mounts.SMBConnectionManager", Manager)
    record = RepositoryConfig(name="NAS", type="smb", server="nas", share="share", username="user")
    with repositories.repository_operation_context(record, "secret") as operation_record:
        assert order == ["connect"]
        assert operation_record is record
    assert order == expected


def test_smb_operation_context_always_mounts_and_never_selects_rclone(monkeypatch, tmp_path: Path) -> None:
    """SMB has one data path: a mounted filesystem. The context yields a local mounted
    record (no rclone provider bypass, no unchanged UNC record), so this decision cannot
    silently regress."""
    from backer.serverless import repositories

    monkeypatch.setattr(repositories.sys, "platform", "linux", raising=False)

    @contextmanager
    def mount(*_args):
        yield tmp_path / "mounted"

    monkeypatch.setattr("backer.core.mounts.smb_mount_context", mount)
    record = RepositoryConfig(name="NAS", type="smb", server="nas", share="share", username="user", path="sub/dir")

    with repositories.repository_operation_context(record, "smb-pass") as operation_record:
        # Mounted: Kopia sees a local path under the mount, not the raw UNC SMB record.
        assert operation_record.type == "local"
        assert operation_record.path == str(tmp_path / "mounted" / "sub" / "dir")


def test_smb_mount_context_refuses_without_root(monkeypatch) -> None:
    from backer.core import mounts

    monkeypatch.setattr(mounts.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(mounts, "find_existing_cifs_mount", lambda *_: None)
    monkeypatch.setattr(mounts, "gvfs_available", lambda: False)
    monkeypatch.setattr(mounts, "sudo_available", lambda: False)
    with pytest.raises(RuntimeError, match="root privileges"):
        with mounts.smb_mount_context("nas", "share", "user", "secret", cifs_check=lambda: True):
            pass


def test_smb_mount_context_fails_when_unmount_fails_and_removes_credentials(monkeypatch, tmp_path: Path) -> None:
    from backer.core import mounts

    monkeypatch.setattr(mounts.os, "geteuid", lambda: 0, raising=False)
    mount_point = tmp_path / "mount"
    credentials = tmp_path / "credentials"
    monkeypatch.setattr(mounts.tempfile, "mkdtemp", lambda **_: str(mount_point))
    monkeypatch.setattr(
        mounts.tempfile, "mkstemp", lambda **_: (os.open(credentials, os.O_CREAT | os.O_WRONLY), str(credentials))
    )
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        return type("Result", (), {"returncode": 0 if command[0] == "mount" else 1, "stderr": "busy"})()

    monkeypatch.setattr(mounts.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="unmount"):
        with mounts.smb_mount_context("nas", "share", "user", "secret", cifs_check=lambda: True):
            pass
    assert calls[-1][0] == "umount"
    assert not credentials.exists()


def test_smb_mount_context_uses_passwordless_sudo_kernel_mount_for_nonroot(monkeypatch, tmp_path: Path) -> None:
    from backer.core import mounts

    monkeypatch.setattr(mounts.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(mounts.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(mounts.os, "getgid", lambda: 1000, raising=False)
    monkeypatch.setattr(mounts, "find_existing_cifs_mount", lambda *_: None)
    monkeypatch.setattr(mounts, "sudo_available", lambda: True)
    monkeypatch.setattr(mounts, "gvfs_available", lambda: True)
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append(command)
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(mounts.subprocess, "run", run)
    with mounts.smb_mount_context("nas", "share", "user", "secret", cifs_check=lambda: True) as mount_point:
        assert mount_point.exists()

    assert commands[0][:3] == ["sudo", "-n", "mount"]
    assert commands[1][:3] == ["sudo", "-n", "umount"]
    assert "uid=1000" in commands[0][-1] and "gid=1000" in commands[0][-1]
    assert all("secret" not in " ".join(command) for command in commands)


def test_system_run_refuses_interactive_only_smb_repository(monkeypatch, tmp_path: Path) -> None:
    from backer.core.config import BackerConfig, JobConfig, RepositoryConfig, SourceConfig
    from backer.serverless import runs

    monkeypatch.setattr(runs.sys, "platform", "win32", raising=False)

    config = BackerConfig(
        repositories={
            "repo": RepositoryConfig(
                name="NAS",
                type="smb",
                server="nas",
                share="backups",
                username="backup",
                passphrase_ref="pass",
                use_existing_session=True,
            )
        },
        jobs={"nightly": JobConfig(repository="repo", source=SourceConfig(path=str(tmp_path)))},
    )
    monkeypatch.setattr(runs.keystore, "get", lambda reference, **_kwargs: "repo-pass" if reference == "pass" else None)
    monkeypatch.setattr(runs, "get_data_dir", lambda: tmp_path)

    result = runs._run_local_job(config, "nightly", run_as_system=True)

    assert not result["success"]
    assert "interactive-only" in result["errors"][0]


def _gvfs_env(monkeypatch, tmp_path: Path) -> Path:
    """Point the gvfs helpers at a fake FUSE directory with a session bus present."""
    from backer.core import mounts

    runtime = tmp_path / "runtime"
    gvfs = runtime / "gvfs"
    gvfs.mkdir(parents=True)
    (runtime / "bus").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(mounts.shutil, "which", lambda name: "/usr/bin/gio" if name == "gio" else None)
    monkeypatch.setattr(mounts.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(mounts, "find_existing_cifs_mount", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mounts, "sudo_available", lambda: False)
    return gvfs


def test_find_existing_cifs_mount_matches_case_and_octal_escapes(tmp_path: Path) -> None:
    from backer.core import mounts

    proc_mounts = tmp_path / "mounts"
    proc_mounts.write_text(
        "tmpfs /tmp tmpfs rw 0 0\n"
        "//OtherNas/share /mnt/other cifs rw 0 0\n"
        "//NAS/Backups /mnt/my\\040share cifs rw,username=user 0 0\n",
        encoding="utf-8",
    )

    assert mounts.find_existing_cifs_mount("nas", "backups", str(proc_mounts)) == Path("/mnt/my share")
    assert mounts.find_existing_cifs_mount("nas", "missing", str(proc_mounts)) is None


def test_smb_mount_context_reuses_existing_mount_without_unmounting(monkeypatch, tmp_path: Path) -> None:
    from backer.core import mounts

    existing = tmp_path / "already"
    existing.mkdir()
    monkeypatch.setattr(mounts, "find_existing_cifs_mount", lambda *_args, **_kwargs: existing)
    calls: list[list[str]] = []
    monkeypatch.setattr(mounts.subprocess, "run", lambda command, **_: calls.append(command))

    with mounts.smb_mount_context("nas", "share", "user", "secret", cifs_check=lambda: True) as mount_point:
        assert mount_point == existing

    assert calls == []
    assert existing.exists()


def test_smb_mount_context_mounts_through_gvfs_when_not_root(monkeypatch, tmp_path: Path) -> None:
    from backer.core import mounts

    gvfs = _gvfs_env(monkeypatch, tmp_path)
    entry = gvfs / "smb-share:domain=WORKGROUP,server=nas,share=backups"
    commands: list[list[str]] = []
    stdin: list[str] = []

    def run(command, **kwargs):
        commands.append(command)
        stdin.append(kwargs["input"])
        entry.mkdir()
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(mounts.subprocess, "run", run)

    with mounts.smb_mount_context("NAS", "Backups", "user", "secret", "corp", cifs_check=lambda: False) as path:
        assert path == entry

    assert commands == [["gio", "mount", "smb://NAS/Backups"]]
    assert stdin == ["user\ncorp\nsecret\n"]
    assert "secret" not in " ".join(commands[0])
    # Session-scoped: the gvfs mount is left in place, exactly like a file manager.
    assert entry.exists()


def test_gvfs_mount_writes_empty_domain_line_and_reuses_existing_entry(monkeypatch, tmp_path: Path) -> None:
    from backer.core import mounts

    gvfs = _gvfs_env(monkeypatch, tmp_path)
    stdin: list[str] = []

    def run(command, **kwargs):
        stdin.append(kwargs["input"])
        (gvfs / "smb-share:server=nas,share=backups").mkdir()
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(mounts.subprocess, "run", run)
    assert mounts.gvfs_mount("nas", "backups", "user", "secret", None).name.endswith("share=backups")
    assert stdin == ["user\n\nsecret\n"]

    # Already mounted: no second gio invocation.
    monkeypatch.setattr(mounts.subprocess, "run", lambda *a, **k: pytest.fail("gio ran for a mounted share"))
    assert mounts.gvfs_mount("NAS", "BACKUPS", "user", "secret", None).name.endswith("share=backups")


def test_gvfs_mount_failure_ignores_exit_code_and_hides_password(monkeypatch, tmp_path: Path) -> None:
    from backer.core import mounts

    _gvfs_env(monkeypatch, tmp_path)

    def run(command, **_kwargs):
        # gio exits 0 even though authentication failed and nothing was mounted.
        return CompletedProcess(command, 0, "Authentication Required\nUser: Password: secret", "")

    monkeypatch.setattr(mounts.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="Login failed - invalid username or password") as error:
        with mounts.smb_mount_context("nas", "backups", "user", "secret", cifs_check=lambda: True):
            pytest.fail("a failed gvfs mount must not yield a path")
    assert "secret" not in str(error.value)


def test_smb_mount_context_refuses_when_gio_or_bus_missing(monkeypatch, tmp_path: Path) -> None:
    from backer.core import mounts

    _gvfs_env(monkeypatch, tmp_path)
    monkeypatch.setattr(mounts.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="Install gvfs"):
        with mounts.smb_mount_context("nas", "backups", "user", "secret", cifs_check=lambda: True):
            pytest.fail("no transport available")

    monkeypatch.setattr(mounts.shutil, "which", lambda name: "/usr/bin/gio" if name == "gio" else None)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "no-bus"))
    with pytest.raises(RuntimeError, match="Install gvfs"):
        with mounts.smb_mount_context("nas", "backups", "user", "secret", cifs_check=lambda: True):
            pytest.fail("no session bus, no gvfs")


def _local_run_config(tmp_path: Path) -> BackerConfig:
    return BackerConfig(
        agent_id="agent-one",
        repositories={
            "repo": RepositoryConfig(name="Repo", type="local", path=str(tmp_path / "repo"), passphrase_ref="pass")
        },
        jobs={"nightly": JobConfig(repository="repo", source=SourceConfig(path=str(tmp_path / "src")))},
    )


def _stub_preflight(monkeypatch, status: str = "present", unique_id: str = "uid", message: str = "") -> None:
    monkeypatch.setattr("backer.core.keystore.get", lambda *_args, **_kwargs: "repo-pass")
    monkeypatch.setattr("backer.serverless.runs.probe", lambda *_args: (status, unique_id, message))


@pytest.mark.parametrize("success", [True, False])
def test_local_run_record_carries_the_backend_result(monkeypatch, tmp_path: Path, success: bool) -> None:
    """bytes/files/errors must live in the local record; the GUI and job history read nothing else."""
    import json

    from backer.serverless.runs import run_local_job
    from backer.serverless.store import read_runs

    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path))
    _stub_preflight(monkeypatch)
    monkeypatch.setattr(
        "backer.serverless.runs.run_backup",
        lambda *_args, **_kwargs: {
            "run_id": "ignored",
            "job_name": "nightly",
            "success": success,
            "bytes_transferred": 75012 if success else 0,
            "files_transferred": 5 if success else 0,
            "errors": [] if success else ["failed to prepare source: no such file or directory"],
            "output": "kopia output",
        },
    )

    report = run_local_job(_local_run_config(tmp_path), "nightly")
    assert report["success"] is success
    run = read_runs(tmp_path, "nightly", 1)[0]
    assert run.result is not None
    assert run.result.success is success
    assert run.result.bytes_transferred == (75012 if success else 0)
    assert run.result.files_transferred == (5 if success else 0)
    assert run.result.duration_seconds >= 0
    assert run.result.errors == ([] if success else ["failed to prepare source: no such file or directory"])
    stored = json.loads((tmp_path / "last_attempt" / "nightly.json").read_text(encoding="utf-8"))["result"]
    assert stored["bytes_transferred"] == (75012 if success else 0)


def test_progress_never_claims_more_than_the_estimate(monkeypatch, tmp_path: Path) -> None:
    """total_bytes is the previous snapshot's size, so a grown source must not report 95% of a lie."""
    import json

    from backer.serverless import runs

    runs._write_progress(tmp_path, "run-1", bytes_processed=4274834636, total_bytes=800000000, progress_percent=95)
    grown = json.loads((tmp_path / "progress" / "run-1.json").read_text(encoding="utf-8"))
    assert grown["total_bytes"] is None and grown["progress_percent"] is None

    runs._write_progress(tmp_path, "run-1", bytes_processed=400000000, total_bytes=800000000, progress_percent=100)
    within = json.loads((tmp_path / "progress" / "run-1.json").read_text(encoding="utf-8"))
    assert within["total_bytes"] == 800000000 and within["progress_percent"] == 99


def test_unique_id_mismatch_names_the_repointed_repository(monkeypatch, tmp_path: Path) -> None:
    from backer.serverless.runs import run_local_job
    from backer.serverless.store import read_runs

    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path))
    _stub_preflight(monkeypatch, unique_id="found-id")
    config = _local_run_config(tmp_path)
    config.repositories["repo"].unique_id = "expected-id"

    report = run_local_job(config, "nightly")
    message = "; ".join(report["errors"])
    assert "is not the one job 'nightly' was configured against" in message
    assert "expected-id" in message and "found-id" in message
    assert str(tmp_path / "repo") in message
    assert read_runs(tmp_path, "nightly", 1)[0].error_stage == "prepare_destination"
    assert not (tmp_path / "repo").exists()


def test_preflight_failure_reaches_a_reachable_sidecar(monkeypatch, tmp_path: Path) -> None:
    """A wrong passphrase must still land in the sidecar a replacement machine reads."""
    from backer.core.repo_metadata import RepositoryMetadata
    from backer.serverless.runs import run_local_job

    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path))
    RepositoryMetadata(tmp_path / "repo").initialize()
    _stub_preflight(monkeypatch, status="wrong_passphrase", unique_id="", message="invalid repository password")

    report = run_local_job(_local_run_config(tmp_path), "nightly")
    assert not report["success"]
    records = RepositoryMetadata(tmp_path / "repo").get_job_runs("nightly")
    assert len(records) == 1
    assert records[0]["status"] == "failed"
    assert records[0]["error_stage"] == "connect"
    assert records[0]["error"] == "invalid repository password"
    assert records[0]["started_at"].endswith("Z")


def test_preflight_failure_never_creates_a_sidecar_in_an_unknown_destination(monkeypatch, tmp_path: Path) -> None:
    from backer.serverless.runs import run_local_job

    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path))
    _stub_preflight(monkeypatch, status="absent", unique_id="", message="repository not found")

    assert not run_local_job(_local_run_config(tmp_path), "nightly")["success"]
    assert not (tmp_path / "repo").exists()


def test_live_log_frame_writes_readable_lines_while_the_run_proceeds(tmp_path: Path) -> None:
    from backer.serverless import runs

    state: dict[str, object] = {"bytes": 0, "files": None}
    # First frame writes; a tiny follow-up frame is throttled; a big jump writes again.
    runs._live_log_frame(tmp_path, "r1", {"hashed_bytes": 100 * 1024 * 1024, "hashed_files": 3}, state)
    runs._live_log_frame(tmp_path, "r1", {"hashed_bytes": 101 * 1024 * 1024, "hashed_files": 3}, state)
    runs._live_log_frame(tmp_path, "r1", {"hashed_bytes": 300 * 1024 * 1024, "hashed_files": 9}, state)

    lines = (tmp_path / "logs" / "r1.log").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert "3 files" in lines[0] and "MiB" in lines[0]
    assert "9 files" in lines[1]


def test_run_log_keeps_the_tail_and_rotates_per_job(tmp_path: Path) -> None:
    """Kopia puts the cause at the end of the output, and one busy job must not evict another's logs."""
    from backer.serverless import runs
    from backer.serverless.store import append_run

    runs._write_log(tmp_path, "run-long", "nightly", "HEAD" + ("x" * 20000) + "the real kopia error", [])
    written = (tmp_path / "logs" / "run-long.log").read_text(encoding="utf-8")
    assert written.startswith("HEAD")
    assert written.endswith("the real kopia error")
    assert len(written) < 6000

    for index in range(25):
        run_id = f"run-{index:02d}"
        append_run(tmp_path, JobRun("nightly", run_id, JobStatus.SUCCESS, datetime(2026, 9, 1, tzinfo=UTC)))
        runs._write_log(tmp_path, run_id, "nightly", "output", [])
    runs._write_log(tmp_path, "other-job-run", "weekly", "output", [])
    runs._write_log(tmp_path, "run-25", "nightly", "output", [])

    logs = {path.stem for path in (tmp_path / "logs").glob("*.log")}
    assert "other-job-run" in logs
    assert len([name for name in logs if name.startswith("run-")]) == runs.LOGS_KEPT_PER_JOB + 1
