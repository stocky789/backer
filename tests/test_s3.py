from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import pytest
from fastapi.testclient import TestClient

from backer.agent.service import AgentService
from backer.backends.base import BackupDestination, BackupSource
from backer.backends.kopia import KopiaBackend
from backer.backends.s3 import S3ConfigError, kopia_s3_config, parse_s3_config
from backer.server.app import _build_backup_command_payload, create_app
from backer.server.web.auth import get_setup_token


def config(**overrides: object) -> dict[str, object]:
    return {
        "bucket": "backer-test",
        "prefix": "agents/host-one",
        "endpoint": "https://minio.example.test:9000",
        "region": "us-east-1",
        "access_key_id": "test-access-key",
        "secret_access_key": "test-secret-key",
        **overrides,
    }


def test_s3_config_builds_kopia_boundary() -> None:
    result = kopia_s3_config(config())

    assert result["repository"] == "s3://backer-test/agents/host-one"
    assert result["options"] == [
        "--bucket",
        "backer-test",
        "--prefix",
        "agents/host-one",
        "--endpoint",
        "minio.example.test:9000",
        "--region",
        "us-east-1",
    ]
    assert result["environment"] == {
        "AWS_ACCESS_KEY_ID": "test-access-key",
        "AWS_SECRET_ACCESS_KEY": "test-secret-key",
    }
    assert "secret_access_key" not in result["public_config"]
    assert "access_key_id" not in result["public_config"]


def test_http_s3_endpoint_disables_tls() -> None:
    result = kopia_s3_config(config(endpoint="http://minio:9000"))
    assert result["options"][-1] == "--disable-tls"


def test_kopia_s3_secrets_are_environment_only() -> None:
    backend = KopiaBackend({"repository_password": "repo-password", "s3": config()})
    repo_type, arguments = backend._get_repo_type("s3://backer-test/agents/host-one")

    assert repo_type == "s3"
    assert "test-access-key" not in arguments
    assert "test-secret-key" not in arguments
    assert backend._env["AWS_ACCESS_KEY_ID"] == "test-access-key"
    assert backend._env["AWS_SECRET_ACCESS_KEY"] == "test-secret-key"


def test_agent_logs_redact_both_s3_credentials(tmp_path) -> None:
    safe = AgentService("http://server", "agent", "secret", tools_dir=tmp_path)._redact_sensitive_data({"s3": config()})

    assert safe["s3"]["access_key_id"] == "***REDACTED***"
    assert safe["s3"]["secret_access_key"] == "***REDACTED***"


def test_s3_api_encrypts_credentials_and_builds_kopia_agent_payload(tmp_path, monkeypatch) -> None:
    app = create_app(tmp_path)
    monkeypatch.setattr(KopiaBackend, "test_connection", lambda *_: (True, "mocked S3 connection"))
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post(
            "/setup",
            data={
                "username": "owner",
                "display_name": "Owner",
                "password": "test-admin-password",
                "confirm_password": "test-admin-password",
                "setup_token": get_setup_token(),
                "timezone": "Australia/Sydney",
                "public_url": "https://backer.example.test",
            },
        )
        response = client.post(
            "/api/v1/repositories",
            json={
                "name": "Offsite",
                "type": "s3",
                "repository_password": "repo-password",
                "s3": config(),
            },
        )
    assert response.status_code == 200
    repo_id = response.json()["id"]
    storage = app.state.storage
    repo = storage.get_repository(repo_id)
    assert repo is not None
    assert repo["config"] == {
        "s3": {
            "bucket": "backer-test",
            "prefix": "agents/host-one",
            "endpoint": "https://minio.example.test:9000",
            "region": "us-east-1",
        }
    }
    assert "test-secret-key" not in str(storage.list_repositories())
    assert storage.get_repository_provider_credentials(repo_id) == {
        "access_key_id": "test-access-key",
        "secret_access_key": "test-secret-key",
    }

    payload = _build_backup_command_payload(
        {
            "repository_id": repo_id,
            "source_path": "/source",
            "destination_path": "ignored",
        },
        "daily",
        "run-1",
        storage=storage,
    )
    assert payload["destination_path"] == "s3://backer-test/agents/host-one"
    assert payload["repository_options"] == {
        "repository_password": "repo-password",
        "s3": {
            "bucket": "backer-test",
            "prefix": "agents/host-one",
            "endpoint": "https://minio.example.test:9000",
            "region": "us-east-1",
            "access_key_id": "test-access-key",
            "secret_access_key": "test-secret-key",
        },
    }


@pytest.mark.parametrize("field,value", [("bucket", ""), ("endpoint", "minio.example.test"), ("prefix", "../escape")])
def test_s3_config_rejects_incomplete_or_unsafe_values(field: str, value: object) -> None:
    with pytest.raises(S3ConfigError):
        parse_s3_config(config(**{field: value}))


def test_s3_bucket_create_request_signs_without_exposing_secret() -> None:
    request = _s3_bucket_create_request(
        endpoint="http://minio.example.test:9000",
        bucket="backer-test",
        region="us-east-1",
        access_key="test-access-key",
        secret_key="test-secret-key",
        now="20260828T120000Z",
    )

    assert request.full_url == "http://minio.example.test:9000/backer-test"
    assert request.get_method() == "PUT"
    assert request.get_header("X-amz-content-sha256") == hashlib.sha256(b"").hexdigest()
    assert request.get_header("Authorization").startswith(
        "AWS4-HMAC-SHA256 Credential=test-access-key/20260828/us-east-1/s3/aws4_request"
    )
    assert "test-secret-key" not in str(request.header_items())


def _s3_bucket_create_request(
    endpoint: str, bucket: str, region: str, access_key: str, secret_key: str, now: str
) -> Request:
    payload_hash = hashlib.sha256(b"").hexdigest()
    parsed = urlparse(endpoint)
    path = f"/{quote(bucket, safe='-_.~')}"
    headers = f"host:{parsed.netloc}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{now}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    scope = f"{now[:8]}/{region}/s3/aws4_request"
    canonical_request = f"PUT\n{path}\n\n{headers}\n{signed_headers}\n{payload_hash}"
    string_to_sign = (
        "AWS4-HMAC-SHA256\n" + now + "\n" + scope + "\n" + hashlib.sha256(canonical_request.encode()).hexdigest()
    )

    def sign(key: bytes, value: str) -> bytes:
        return hmac.new(key, value.encode(), hashlib.sha256).digest()

    signing_key = sign(sign(sign(sign(f"AWS4{secret_key}".encode(), now[:8]), region), "s3"), "aws4_request")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return Request(
        f"{endpoint.rstrip('/')}{path}",
        data=b"",
        method="PUT",
        headers={
            "Host": parsed.netloc,
            "X-Amz-Content-Sha256": payload_hash,
            "X-Amz-Date": now,
            "Authorization": (
                f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
        },
    )


def _create_s3_bucket(endpoint: str, bucket: str, region: str, access_key: str, secret_key: str) -> None:
    request = _s3_bucket_create_request(
        endpoint, bucket, region, access_key, secret_key, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    try:
        with urlopen(request, timeout=30):
            pass
    except HTTPError as exc:
        if exc.code == 409 and b"BucketAlreadyOwnedByYou" in exc.read():
            return
        raise RuntimeError("S3 test bucket could not be created") from exc


def test_s3_minio_end_to_end(tmp_path: Path) -> None:
    names = (
        "BACKER_TEST_S3_ENDPOINT",
        "BACKER_TEST_S3_BUCKET",
        "BACKER_TEST_S3_ACCESS_KEY",
        "BACKER_TEST_S3_SECRET_KEY",
    )
    if not all(os.getenv(name) for name in names):
        pytest.skip("all BACKER_TEST_S3_* variables are required")
    endpoint, bucket, access_key, secret_key = (os.environ[name] for name in names)
    _create_s3_bucket(endpoint, bucket, "us-east-1", access_key, secret_key)

    backend = KopiaBackend(
        {
            "repository_password": "repository-password",
            "s3": config(
                bucket=bucket,
                prefix="agent/job",
                endpoint=endpoint,
                access_key_id=access_key,
                secret_access_key=secret_key,
            ),
        }
    )
    repository = BackupDestination(f"s3://{bucket}/agent/job")
    source, restored = tmp_path / "source", tmp_path / "restored"
    source.mkdir()
    (source / "keep.txt").write_text("v1")
    (source / "deleted.txt").write_text("remove")

    assert backend.test_connection(repository)[0]
    assert backend.backup(BackupSource(source), repository).success
    (source / "keep.txt").write_text("v2")
    (source / "deleted.txt").unlink()
    assert backend.backup(BackupSource(source), repository).success
    assert len(backend.list_snapshots(repository)) == 2
    assert backend.restore(repository, restored, snapshot="latest").success
    assert next(restored.rglob("keep.txt")).read_text() == "v2"
    assert not list(restored.rglob("deleted.txt"))
    assert backend.prune(repository, keep_last=1).success
    assert backend.check(repository).success
