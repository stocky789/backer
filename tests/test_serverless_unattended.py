import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backer.core.config import BackerConfig, JobConfig, RepositoryConfig, SourceConfig


class _S3Response:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content


def _sidecar_config(source_path: str, *, repository_hint: dict[str, str] | None = None) -> dict[str, object]:
    return {
        "source_path": source_path,
        "source_hostname": "host",
        "source_platform": "win32",
        "kopia_source": f"user@host:{source_path}",
        "excludes": [],
        "subfolder": "Nightly",
        "schedule": None,
        "retention": None,
        "repository_hint": repository_hint or {"type": "local", "path": "repo"},
        "repository_password_hint": None,
        "client_id": "agent-one",
        "enabled": True,
    }


def test_sidecar_job_config_uses_stable_subfolder_and_rejects_secret(tmp_path: Path) -> None:
    from backer.serverless.sidecar import save_job_config

    config = _sidecar_config("C:/Users/me/Documents")
    path = save_job_config(tmp_path, "Nightly: Documents", "agent-one", config, [])

    assert path == tmp_path / ".backer" / "jobs" / "Nightly_ Documents" / "config.json"
    saved = json.loads(path.read_text())
    assert saved["owner_agent_id"] == "agent-one"
    assert saved["config"] == config
    with pytest.raises(ValueError, match="secret"):
        save_job_config(tmp_path, "Other", "agent-one", {"password": "nope"}, [])


def test_adopt_preserves_job_name_and_never_rewrites_owner_sidecar(tmp_path: Path) -> None:
    from backer.serverless.sidecar import adopt_jobs, save_job_config

    saved_config = _sidecar_config("/missing")
    saved_config["excludes"] = ["*.tmp"]
    save_job_config(tmp_path, "Nightly", "old-agent", saved_config, [])
    config = BackerConfig(
        agent_id="new-agent", repositories={"repo": RepositoryConfig(name="Repo", type="local", path=str(tmp_path))}
    )

    adopted = adopt_jobs(config, "repo", tmp_path, ["Nightly"], source_paths={"Nightly": "/local"})

    assert adopted.adopted == ["Nightly"]
    assert adopted.failures == {}
    assert config.jobs["Nightly"] == JobConfig(
        repository="repo", source=SourceConfig(path="/local", excludes=["*.tmp"])
    )
    assert (
        json.loads((tmp_path / ".backer" / "jobs" / "Nightly" / "config.json").read_text())["owner_agent_id"]
        == "old-agent"
    )


def test_due_jobs_records_fire_before_backup_starts(tmp_path: Path) -> None:
    from backer.serverless.schedule import due_jobs

    config = BackerConfig(
        jobs={
            "nightly": JobConfig(repository="repo", source=SourceConfig(path="/data"), schedule={"cron": "0 * * * *"})
        }
    )
    now = datetime(2026, 9, 1, 2, 1, tzinfo=UTC)

    assert due_jobs(config, now, tmp_path) == ["nightly"]
    assert due_jobs(config, now, tmp_path) == []
    assert json.loads((tmp_path / "schedule.json").read_text())["nightly"] == "2026-09-01T02:01:00Z"


def test_serverless_smb_system_reclaims_1219_without_cmdkey(monkeypatch) -> None:
    from backer.core.mounts import SMBConnectionManager

    calls: list[list[str]] = []
    attempts = 0

    def run(command: list[str], **kwargs: object):
        nonlocal attempts
        calls.append(command)
        if command == ["net", "use"]:
            return type("Result", (), {"returncode": 0, "stdout": "OK \\\\nas\\share", "stderr": ""})()
        if command == ["net", "use", "\\\\nas\\share"]:
            return type("Result", (), {"returncode": 0, "stdout": "User name: other-user", "stderr": ""})()
        if command[0:3] == ["net", "use", "\\\\nas\\share"] and "*" in command:
            attempts += 1
            return type(
                "Result", (), {"returncode": 1 if attempts == 1 else 0, "stdout": "", "stderr": "System error 1219"}
            )()
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("backer.core.mounts.subprocess.run", run)
    manager = SMBConnectionManager()

    assert manager.connect_serverless("nas", "share", "backup", "secret", is_system=True)
    assert ["net", "use", "\\\\nas\\share", "/delete", "/y"] in calls
    assert not any(command[0] == "cmdkey" for command in calls)


def test_serverless_smb_reuses_same_user_without_deleting(monkeypatch) -> None:
    from backer.core.mounts import SMBConnectionManager

    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object):
        calls.append(command)
        if command[0:3] == ["net", "use", "\\\\nas\\share"] and "*" in command:
            return type("Result", (), {"returncode": 1, "stdout": "", "stderr": "System error 1219"})()
        if command == ["net", "use"]:
            return type("Result", (), {"returncode": 0, "stdout": "OK \\\\nas\\share", "stderr": ""})()
        return type("Result", (), {"returncode": 0, "stdout": "User name: backup", "stderr": ""})()

    monkeypatch.setattr("backer.core.mounts.subprocess.run", run)
    manager = SMBConnectionManager()
    assert manager.connect_serverless("nas", "share", "backup", "secret")
    assert not manager.serverless_session_created
    assert ["net", "use", "\\\\nas\\share", "/delete", "/y"] not in calls


def test_windows_scheduled_test_cleanup_retains_task_when_stop_cannot_be_verified(monkeypatch) -> None:
    from backer.client import windows_service

    monkeypatch.setattr(windows_service, "is_windows", lambda: True)
    monkeypatch.setattr(windows_service, "_windows_task_state", lambda _task: {"exists": True, "running": True})
    calls = []
    monkeypatch.setattr(
        windows_service.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command) or type("Result", (), {"returncode": 1, "stderr": "denied"})(),
    )

    ok, message = windows_service.remove_local_scheduled_test_task("0123456789ab")

    assert not ok and "credentials were retained" in message
    assert not any("/delete" in command for command in calls)


def test_windows_scheduler_freeze_refuses_a_run_that_starts_between_snapshot_and_mutation(monkeypatch) -> None:
    from backer.client import windows_service

    calls = []
    states = iter([{"exists": True, "running": True}, {"exists": True, "enabled": True, "running": True}])
    monkeypatch.setattr(windows_service, "_windows_task_state", lambda _task: next(states))
    monkeypatch.setattr(
        windows_service.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command) or type("Result", (), {"returncode": 0, "stderr": ""})(),
    )

    freeze = windows_service.prepare_local_scheduler_mutation(
        {"platform": "windows", "task": {"exists": True, "enabled": True, "running": False}}
    )

    assert not freeze.ready and not freeze.restore_failed and "started" in freeze.message
    assert ["schtasks", "/change", "/tn", "BackerLocalSchedule", "/disable"] in calls
    assert ["schtasks", "/change", "/tn", "BackerLocalSchedule", "/enable"] in calls
    assert not any("/delete" in command or "/run" in command for command in calls)


def test_windows_scheduler_freeze_marks_failed_trigger_restore_for_transaction_rollback(monkeypatch) -> None:
    from backer.client import windows_service

    calls = []
    monkeypatch.setattr(windows_service, "_windows_task_state", lambda _task: {"exists": True, "running": True})
    results = iter([0, 1])
    monkeypatch.setattr(
        windows_service.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command)
        or type("Result", (), {"returncode": next(results), "stderr": "denied", "stdout": ""})(),
    )

    freeze = windows_service.prepare_local_scheduler_mutation(
        {"platform": "windows", "task": {"exists": True, "enabled": True, "running": False}}
    )

    assert not freeze.ready and freeze.restore_failed and "restore" in freeze.message
    assert calls[-1] == ["schtasks", "/change", "/tn", "BackerLocalSchedule", "/enable"]


def test_linux_scheduler_freeze_refuses_a_run_that_starts_between_snapshot_and_mutation(monkeypatch) -> None:
    from backer.client import windows_service

    calls = []
    results = iter([0, 3, 0, 0, 0])
    monkeypatch.setattr(
        windows_service.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command)
        or type("Result", (), {"returncode": next(results), "stderr": "", "stdout": ""})(),
    )

    freeze = windows_service.prepare_local_scheduler_mutation(
        {
            "platform": "linux",
            "units": {"service": b"unit", "timer": b"unit"},
            "state": {
                "backer-local.service": {"running": False},
                "backer-local.timer": {"running": True},
            },
        }
    )

    assert not freeze.ready and not freeze.restore_failed and "started" in freeze.message
    assert ["systemctl", "--user", "stop", "backer-local.timer"] in calls
    assert ["systemctl", "--user", "start", "backer-local.timer"] in calls
    assert not any("disable" in command or "daemon-reload" in command for command in calls)


def test_linux_scheduler_freeze_marks_failed_trigger_restore_for_transaction_rollback(monkeypatch) -> None:
    from backer.client import windows_service

    calls = []
    results = iter([0, 3, 0, 1])
    monkeypatch.setattr(
        windows_service.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command)
        or type("Result", (), {"returncode": next(results), "stderr": "denied", "stdout": ""})(),
    )

    freeze = windows_service.prepare_local_scheduler_mutation(
        {
            "platform": "linux",
            "units": {"service": b"unit", "timer": b"unit"},
            "state": {"backer-local.service": {"running": False}, "backer-local.timer": {"running": True}},
        }
    )

    assert not freeze.ready and freeze.restore_failed and "restore" in freeze.message
    assert calls[-1] == ["systemctl", "--user", "start", "backer-local.timer"]


def test_windows_scheduler_freeze_refuses_a_successful_noop_disable(monkeypatch) -> None:
    from backer.client import windows_service

    calls = []
    states = iter([{"exists": True, "enabled": True, "running": False}, {"exists": True, "enabled": True}])
    monkeypatch.setattr(windows_service, "_windows_task_state", lambda _task: next(states))
    monkeypatch.setattr(
        windows_service.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command) or type("Result", (), {"returncode": 0, "stderr": ""})(),
    )

    freeze = windows_service.prepare_local_scheduler_mutation(
        {"platform": "windows", "task": {"exists": True, "enabled": True, "running": False}}
    )

    assert not freeze.ready and not freeze.restore_failed and "disabled" in freeze.message
    assert not any("/delete" in command or "/run" in command for command in calls)


def test_linux_scheduler_freeze_refuses_a_successful_noop_stop(monkeypatch) -> None:
    from backer.client import windows_service

    calls = []
    results = iter([0, 0, 0, 0])
    monkeypatch.setattr(
        windows_service.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command)
        or type("Result", (), {"returncode": next(results), "stderr": "", "stdout": ""})(),
    )

    freeze = windows_service.prepare_local_scheduler_mutation(
        {
            "platform": "linux",
            "units": {"service": b"unit", "timer": b"unit"},
            "state": {"backer-local.service": {"running": False}, "backer-local.timer": {"running": True}},
        }
    )

    assert not freeze.ready and not freeze.restore_failed and "inactive" in freeze.message
    assert not any("disable" in command or "daemon-reload" in command for command in calls)


def test_windows_scheduler_freeze_refuses_trigger_reactivation_before_mutation(monkeypatch) -> None:
    from backer.client import windows_service

    states = iter(
        [
            {"exists": True, "enabled": False, "running": False},
            {"exists": True, "enabled": True, "running": False},
            {"exists": True, "enabled": True},
        ]
    )
    monkeypatch.setattr(windows_service, "_windows_task_state", lambda _task: next(states))
    monkeypatch.setattr(
        windows_service.subprocess,
        "run",
        lambda _command, **_kwargs: type("Result", (), {"returncode": 0, "stderr": ""})(),
    )

    freeze = windows_service.prepare_local_scheduler_mutation(
        {"platform": "windows", "task": {"exists": True, "enabled": True, "running": False}}
    )

    assert not freeze.ready and "changed" in freeze.message


def test_linux_scheduler_freeze_refuses_timer_reactivation_before_mutation(monkeypatch) -> None:
    from backer.client import windows_service

    results = iter([0, 3, 3, 0, 0, 0])
    monkeypatch.setattr(
        windows_service.subprocess,
        "run",
        lambda _command, **_kwargs: type("Result", (), {"returncode": next(results), "stderr": ""})(),
    )

    freeze = windows_service.prepare_local_scheduler_mutation(
        {
            "platform": "linux",
            "units": {"service": b"unit", "timer": b"unit"},
            "state": {"backer-local.service": {"running": False}, "backer-local.timer": {"running": True}},
        }
    )

    assert not freeze.ready and "changed" in freeze.message


def test_linux_scheduled_test_cleanup_retains_service_when_stop_fails(monkeypatch) -> None:
    from backer.client import windows_service

    monkeypatch.setattr(windows_service, "is_windows", lambda: False)
    monkeypatch.setattr(windows_service.Path, "exists", lambda _self: True)
    monkeypatch.setattr(windows_service.Path, "unlink", lambda *_args, **_kwargs: pytest.fail("must retain service"))
    responses = iter(
        [
            type("Result", (), {"returncode": 1, "stderr": "denied", "stdout": ""})(),
            type("Result", (), {"returncode": 0, "stderr": "", "stdout": "active\n"})(),
        ]
    )
    monkeypatch.setattr(windows_service.subprocess, "run", lambda *_args, **_kwargs: next(responses))

    ok, message = windows_service.remove_local_systemd_test_service("0123456789ab")

    assert not ok and "credentials were retained" in message


def test_rescope_repository_secrets_to_machine(monkeypatch) -> None:
    from backer.serverless.repositories import rescope_secrets_for_system

    values = {("pass", False): "passphrase", ("store", False): '{"key":"value"}'}
    writes: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        "backer.serverless.repositories.keystore.get",
        lambda key, *, machine_scope=False: values.get((key, machine_scope)),
    )

    def put(key: str, value: str, *, machine_scope: bool = False) -> str:
        writes.append((key, value, machine_scope))
        values[key, machine_scope] = value
        return "file"

    monkeypatch.setattr("backer.serverless.repositories.keystore.put", put)
    config = BackerConfig(
        repositories={
            "repo": RepositoryConfig(
                name="Repo", type="local", path="/repo", passphrase_ref="pass", storage_password_ref="store"
            )
        }
    )

    rescope_secrets_for_system(config)

    assert writes == [("pass", "passphrase", True), ("store", '{"key":"value"}', True)]
    assert config.repositories["repo"].scope == "machine"


def test_due_lock_is_nonblocking(tmp_path: Path) -> None:
    from backer.serverless.schedule import run_lock

    with run_lock(tmp_path) as first:
        assert first
        with run_lock(tmp_path) as second:
            assert not second


def test_sidecar_hint_drops_absolute_destination_path(tmp_path: Path) -> None:
    from backer.serverless.sidecar import save_job_config

    path = save_job_config(
        tmp_path,
        "Nightly",
        "agent",
        _sidecar_config("C:/data", repository_hint={"type": "local", "path": "C:/repo"}),
        [],
    )

    assert "path" not in json.loads(path.read_text())["config"]["repository_hint"]


def test_sidecar_document_requires_full_adoption_config_and_rejects_secret_values() -> None:
    from backer.serverless.sidecar import build_job_document

    config = {
        "source_path": "/data",
        "source_hostname": "host",
        "source_platform": "linux",
        "kopia_source": "user@host:/data",
        "excludes": [],
        "subfolder": "nightly",
        "schedule": {"cron": "0 2 * * *"},
        "retention": None,
        "repository_hint": {"type": "s3", "bucket": "bucket"},
        "repository_password_hint": "stored elsewhere",
        "client_id": "agent-one",
        "enabled": True,
    }

    document = build_job_document("nightly", "agent-one", config, ["repo-pass", "storage-secret"])

    assert document["config"] == config
    with pytest.raises(ValueError, match="secret"):
        build_job_document("nightly", "agent-one", {**config, "repository_password_hint": "repo-pass"}, ["repo-pass"])
    with pytest.raises(ValueError, match="missing"):
        build_job_document(
            "nightly", "agent-one", {key: value for key, value in config.items() if key != "schedule"}, []
        )


def test_s3_sidecar_signs_encoded_copy_and_cleans_temp_after_copy_failure(monkeypatch) -> None:
    from backer.serverless.s3_sidecar import S3Sidecar

    requests: list[tuple[str, str, dict[str, str]]] = []

    def request(method: str, url: str, **kwargs: object) -> _S3Response:
        headers = kwargs["headers"]
        assert isinstance(headers, dict)
        requests.append((method, url, headers))
        return _S3Response(500 if method == "PUT" and "x-amz-copy-source" in headers else 200)

    monkeypatch.setattr("backer.serverless.s3_sidecar.requests.request", request)
    monkeypatch.setattr("backer.serverless.s3_sidecar.uuid4", lambda: type("UUID", (), {"hex": "12345678"})())
    sidecar = S3Sidecar(
        {"bucket": "bucket name", "prefix": "repo path", "endpoint": "https://s3.example"},
        {"access_key_id": "access", "secret_access_key": "storage-secret"},
    )

    with pytest.raises(RuntimeError, match="500"):
        sidecar.put_atomic(".backer/jobs/a file/config.json", b"payload")

    assert [item[0] for item in requests] == ["PUT", "PUT", "DELETE"]
    copy_headers = requests[1][2]
    assert (
        copy_headers["x-amz-copy-source"] == "/bucket%20name/repo%20path/.backer/jobs/a%20file/config.json.12345678.tmp"
    )
    assert "x-amz-copy-source" in copy_headers["Authorization"]
    assert all("storage-secret" not in url for _, url, _ in requests)


def test_s3_sidecar_lists_and_gets_without_putting_secrets_in_urls(monkeypatch) -> None:
    from backer.serverless.s3_sidecar import S3Sidecar

    requests: list[tuple[str, str]] = []
    responses = iter(
        [
            _S3Response(
                200,
                b"<ListBucketResult><Contents><Key>prefix/.backer/jobs/a/config.json</Key></Contents></ListBucketResult>",
            ),
            _S3Response(404),
        ]
    )

    def request(method: str, url: str, **_: object) -> _S3Response:
        requests.append((method, url))
        return next(responses)

    monkeypatch.setattr("backer.serverless.s3_sidecar.requests.request", request)
    sidecar = S3Sidecar(
        {"bucket": "bucket", "prefix": "prefix", "endpoint": "https://s3.example"},
        {"access_key_id": "access", "secret_access_key": "storage-secret"},
    )

    assert sidecar.list(".backer/jobs/") == ["prefix/.backer/jobs/a/config.json"]
    assert sidecar.get(".backer/jobs/missing.json") is None
    assert [method for method, _ in requests] == ["GET", "GET"]
    assert all("storage-secret" not in url and "access" not in url for _, url in requests)


def test_s3_adoption_updates_only_local_config() -> None:
    from backer.serverless.sidecar import adopt_documents

    config = BackerConfig(
        agent_id="new",
        repositories={"repo": RepositoryConfig(name="Repo", type="s3", bucket="b", endpoint="https://s3.example")},
    )
    document = {
        "schema_version": "2",
        "job_name": "nightly",
        "owner_agent_id": "old",
        "config": {"source_path": "/old", "excludes": ["*.tmp"]},
    }

    assert adopt_documents(
        config, "repo", {"nightly": document}, ["nightly"], source_paths={"nightly": "/new"}
    ).adopted == ["nightly"]
    assert config.jobs["nightly"].source.path == "/new"


def test_runner_s3_sidecar_uses_full_document_and_owner_cannot_be_replaced(monkeypatch) -> None:
    from types import SimpleNamespace

    from backer.core.runner import _write_repo_metadata

    stored: dict[str, bytes] = {}

    class Sidecar:
        def __init__(self, *_: object) -> None:
            pass

        def get(self, key: str) -> bytes | None:
            return stored.get(key)

        def put_atomic(self, key: str, data: bytes) -> None:
            stored[key] = data

    monkeypatch.setattr("backer.serverless.s3_sidecar.S3Sidecar", Sidecar)
    job = {
        "serverless": True,
        "job_name": "nightly",
        "source_path": "/data",
        "excludes": ["*.tmp"],
        "schedule": {"cron": "0 2 * * *"},
        "retention": {"keep_latest": 3},
        "repository_hint": {"type": "s3", "bucket": "bucket", "endpoint": "https://s3.example"},
        "repository_options": {"repository_password": "repo-pass", "s3": {"secret_access_key": "storage-secret"}},
    }
    result = SimpleNamespace(success=True, bytes_transferred=1, files_transferred=1, errors=[])
    now = datetime(2026, 9, 1, tzinfo=UTC)

    _write_repo_metadata(job, "s3://bucket", "kopia", result, now, now, None, "owner")

    job_key = ".backer/jobs/nightly/config.json"
    run_key = f".backer/jobs/nightly/runs/{job.get('run_id', 'unknown')}.json"
    document = json.loads(stored[job_key])
    assert set(document["config"]) == {
        "source_path",
        "source_hostname",
        "source_platform",
        "kopia_source",
        "excludes",
        "subfolder",
        "schedule",
        "retention",
        "repository_hint",
        "repository_password_hint",
        "client_id",
        "enabled",
    }
    assert "repo-pass" not in stored[job_key].decode()
    assert "storage-secret" not in stored[job_key].decode()
    assert set(stored) == {".backer/metadata.json", ".backer/agents/owner.json", job_key, run_key}

    # A machine that adopted this job may not rewrite the owner's document, but its run
    # history must still reach the repository - that is the only store an adopter can read.
    stored[job_key] = json.dumps({**document, "owner_agent_id": "other"}).encode()
    _write_repo_metadata({**job, "run_id": "later"}, "s3://bucket", "kopia", result, now, now, None, "owner")

    assert json.loads(stored[job_key])["owner_agent_id"] == "other"
    assert ".backer/jobs/nightly/runs/later.json" in stored
    assert json.loads(stored[".backer/jobs/nightly/runs/later.json"])["status"] == "success"


def test_repo_adopt_s3_reads_prefixed_sidecars_and_saves_only_local_config(monkeypatch, tmp_path: Path) -> None:
    from click.testing import CliRunner

    from backer.cli import main

    config = BackerConfig(
        agent_id="new-agent",
        repositories={
            "repo": RepositoryConfig(
                id="repo",
                name="Repo",
                type="s3",
                bucket="bucket",
                prefix="backer-data",
                endpoint="https://s3.example",
                passphrase_ref="passphrase",
                storage_password_ref="storage",
            )
        },
    )
    config.save(tmp_path / "config.yaml")
    monkeypatch.setattr("backer.core.paths.get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "backer.core.keystore.get",
        lambda key, **_: {
            "passphrase": "repo-pass",
            "storage": '{"access_key_id":"access","secret_access_key":"secret"}',
        }[key],
    )
    monkeypatch.setattr("backer.serverless.repositories.probe", lambda *_: ("present", "unique", ""))
    operations: list[tuple[str, str]] = []

    class Sidecar:
        def __init__(self, *_: object) -> None:
            pass

        def list(self, prefix: str) -> list[str]:
            operations.append(("list", prefix))
            return ["backer-data/.backer/jobs/nightly/config.json"]

        def get(self, key: str) -> bytes:
            operations.append(("get", key))
            return (
                b'{"schema_version":"2","job_name":"nightly","owner_agent_id":"old",'
                b'"config":{"source_path":"/old","excludes":["*.tmp"]}}'
            )

        def put_atomic(self, key: str, _: bytes) -> None:
            operations.append(("put", key))

    monkeypatch.setattr("backer.serverless.s3_sidecar.S3Sidecar", Sidecar)

    result = CliRunner().invoke(main, ["repo", "adopt", "Repo", "--all", "--source", "nightly=/new"])

    assert result.exit_code == 0, result.output
    assert operations == [("list", ".backer/jobs/"), ("get", ".backer/jobs/nightly/config.json")]
    saved = BackerConfig.load(tmp_path / "config.yaml")
    assert saved.jobs["nightly"].source.path == "/new"


def _document(source_path: str, **overrides: object) -> dict[str, object]:
    from backer.serverless.sidecar import build_job_document

    config = {**_sidecar_config(source_path), **overrides.pop("config", {})}
    return {**build_job_document("Nightly", "old-agent", config, []), **overrides}


def _config(**jobs: JobConfig) -> BackerConfig:
    return BackerConfig(
        agent_id="new-agent",
        repositories={"repo": RepositoryConfig(name="Repo", type="local", path="/repo")},
        jobs=jobs,
    )


def test_adoption_warns_loudly_when_the_former_machine_source_is_not_here(tmp_path: Path) -> None:
    """[11]/[26]: the job adopts, but the operator is told to remap it before 2am."""
    from backer.serverless.sidecar import adopt_documents

    document = _document(
        "C:/Users/matt/Documents",
        config={"repository_hint": {"type": "smb", "server": "nas"}},
    )
    config = _config()

    outcome = adopt_documents(config, "repo", {"Nightly": document}, ["Nightly"])

    assert outcome.adopted == ["Nightly"]
    assert outcome.failures == {}
    warning = "\n".join(outcome.warnings)
    assert "C:/Users/matt/Documents" in warning
    assert "host/win32" in warning
    assert '--source "Nightly=<local path>"' in warning
    assert '"server": "nas"' in warning
    # A supplied remap is the operator's answer: no warning.
    assert adopt_documents(
        _config(), "repo", {"Nightly": document}, ["Nightly"], source_paths={"Nightly": str(tmp_path)}
    ).warnings == []


def test_adoption_refuses_to_overwrite_a_local_job_without_replace_existing() -> None:
    """[15]: adopt silently repointed an existing job's source."""
    from backer.serverless.sidecar import adopt_documents

    document = _document("/srv/data")
    config = _config(Nightly=JobConfig(repository="repo", source=SourceConfig(path="/local/keep")))

    refused = adopt_documents(config, "repo", {"Nightly": document}, ["Nightly"])

    assert refused.adopted == []
    assert "already exists" in refused.failures["Nightly"]
    assert "/local/keep" in refused.failures["Nightly"]
    assert config.jobs["Nightly"].source.path == "/local/keep"

    replaced = adopt_documents(config, "repo", {"Nightly": document}, ["Nightly"], replace_existing=True)

    assert replaced.adopted == ["Nightly"]
    assert config.jobs["Nightly"].source.path == "/srv/data"
    assert any("/local/keep" in warning for warning in replaced.warnings)


def test_adoption_of_many_jobs_keeps_the_good_ones(tmp_path: Path) -> None:
    """[12]: one unresolvable job used to abort the whole --all run."""
    from backer.serverless.sidecar import adopt_jobs, save_job_config

    save_job_config(tmp_path, "Good", "old-agent", _sidecar_config(str(tmp_path)), [])
    broken = tmp_path / ".backer" / "jobs" / "Broken" / "config.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("{not json", encoding="utf-8")
    config = _config()

    outcome = adopt_jobs(config, "repo", tmp_path, ["Good", "Broken", "Absent"])

    assert outcome.adopted == ["Good"]
    assert set(outcome.failures) == {"Broken", "Absent"}
    assert "unreadable" in outcome.failures["Broken"]
    assert list(config.jobs) == ["Good"]


def test_adoption_reads_a_job_from_its_own_sidecar_folder(tmp_path: Path) -> None:
    """[12]: discover_all merges Agents/<job>/.backer trees, so adopt must read them too."""
    from backer.serverless.sidecar import adopt_jobs, save_job_config

    save_job_config(tmp_path / "Agents" / "Legacy SMB Job", "Legacy SMB Job", "old-agent", _sidecar_config("/srv"), [])
    config = _config()

    outcome = adopt_jobs(
        config, "repo", tmp_path, ["Legacy SMB Job"], job_folders={"Legacy SMB Job": "Agents/Legacy SMB Job"}
    )

    assert outcome.adopted == ["Legacy SMB Job"]


def test_adoption_checks_the_schema_version() -> None:
    """[38]: a 0.8 document adopted silently as a stub; a newer one must be refused."""
    from backer.serverless.sidecar import adopt_documents

    legacy = {"schema_version": "1", "job_name": "Nightly", "config": {"source_path": "/srv/old"}}
    future = {"schema_version": "3", "job_name": "Nightly", "config": {"source_path": "/srv/old"}}

    adopted = adopt_documents(_config(), "repo", {"Nightly": legacy}, ["Nightly"], source_paths={"Nightly": "/here"})
    assert adopted.adopted == ["Nightly"]
    assert any("pre-0.9" in warning for warning in adopted.warnings)

    refused = adopt_documents(_config(), "repo", {"Nightly": future}, ["Nightly"], source_paths={"Nightly": "/here"})
    assert refused.adopted == []
    assert "schema version 3" in refused.failures["Nightly"]


def test_a_disabled_job_is_recorded_and_adopts_disabled() -> None:
    """[24]: 'enabled' was never written, so a paused job woke up on the new machine."""
    from backer.serverless.sidecar import adopt_documents, build_serverless_job_document

    document = build_serverless_job_document({"enabled": False}, "Nightly", "/srv/data", "old-agent")

    assert document["config"]["enabled"] is False
    config = _config()
    adopt_documents(config, "repo", {"Nightly": document}, ["Nightly"], source_paths={"Nightly": "/here"})
    assert config.jobs["Nightly"].enabled is False


@pytest.mark.parametrize(
    "key",
    ["aws_secret_access_key", "AWS_ACCESS_KEY_ID", "access-key", "connection-string", "auth_header", "pwd", "creds"],
)
def test_credential_shaped_keys_never_reach_the_sidecar(key: str) -> None:
    """[25]: the sidecar is plain JSON on a share."""
    from backer.serverless.sidecar import build_job_document

    config = {**_sidecar_config("/srv"), "repository_hint": {"type": "s3", key: "hunter2"}}

    with pytest.raises(ValueError, match="secret"):
        build_job_document("Nightly", "agent-one", config, [])


def test_an_unchanged_job_document_keeps_its_updated_at(tmp_path: Path) -> None:
    """[33]: every run rewrote config.json and moved updated_at for nothing."""
    from backer.serverless.sidecar import save_job_config

    config = _sidecar_config("/srv")
    path = save_job_config(tmp_path, "Nightly", "agent-one", config, [])
    first = json.loads(path.read_text())
    mtime = path.stat().st_mtime_ns

    save_job_config(tmp_path, "Nightly", "agent-one", dict(config), [])

    assert json.loads(path.read_text()) == first
    assert path.stat().st_mtime_ns == mtime

    save_job_config(tmp_path, "Nightly", "agent-one", {**config, "excludes": ["*.tmp"]}, [])
    changed = json.loads(path.read_text())
    assert changed["updated_at"] != first["updated_at"]
    assert changed["created_at"] == first["created_at"]
