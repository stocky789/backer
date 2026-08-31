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

    assert adopted == ["Nightly"]
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
        "job_name": "nightly",
        "owner_agent_id": "old",
        "config": {"source_path": "/old", "excludes": ["*.tmp"]},
    }

    assert adopt_documents(config, "repo", {"nightly": document}, ["nightly"], source_paths={"nightly": "/new"}) == [
        "nightly"
    ]
    assert config.jobs["nightly"].source.path == "/new"


def test_runner_s3_sidecar_uses_full_document_and_owner_cannot_be_replaced(monkeypatch) -> None:
    from types import SimpleNamespace

    from backer.core.runner import _write_repo_metadata

    stored: list[bytes] = []

    class Sidecar:
        existing: bytes | None = None

        def __init__(self, *_: object) -> None:
            pass

        def get(self, _: str) -> bytes | None:
            return self.existing

        def put_atomic(self, _: str, data: bytes) -> None:
            stored.append(data)

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

    document = json.loads(stored[0])
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
    }
    assert "repo-pass" not in stored[0].decode()
    assert "storage-secret" not in stored[0].decode()
    Sidecar.existing = json.dumps({**document, "owner_agent_id": "other"}).encode()
    _write_repo_metadata(job, "s3://bucket", "kopia", result, now, now, None, "owner")
    assert len(stored) == 1


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
            return b'{"job_name":"nightly","owner_agent_id":"old","config":{"source_path":"/old","excludes":["*.tmp"]}}'

        def put_atomic(self, key: str, _: bytes) -> None:
            operations.append(("put", key))

    monkeypatch.setattr("backer.serverless.s3_sidecar.S3Sidecar", Sidecar)

    result = CliRunner().invoke(main, ["repo", "adopt", "Repo", "--all", "--source", "nightly=/new"])

    assert result.exit_code == 0, result.output
    assert operations == [("list", ".backer/jobs/"), ("get", ".backer/jobs/nightly/config.json")]
    saved = BackerConfig.load(tmp_path / "config.yaml")
    assert saved.jobs["nightly"].source.path == "/new"
