"""Opt-in S3 files integration; uses a unique prefix and deletes only its own objects."""

import os
from pathlib import Path
from uuid import uuid4

import pytest

from backer.backends.base import BackupDestination, BackupSource
from backer.backends.s3_files import S3FilesBackend
from backer.serverless.s3_sidecar import S3Sidecar
from tests.test_s3 import _create_s3_bucket


def test_s3_files_live_roundtrip(tmp_path: Path) -> None:
    names = (
        "BACKER_TEST_S3_ENDPOINT", "BACKER_TEST_S3_BUCKET", "BACKER_TEST_S3_ACCESS_KEY", "BACKER_TEST_S3_SECRET_KEY",
    )
    if not all(os.getenv(name) for name in names):
        pytest.skip("all BACKER_TEST_S3_* variables are required")
    endpoint, bucket, access_key, secret_key = (os.environ[name] for name in names)
    _create_s3_bucket(endpoint, bucket, "us-east-1", access_key, secret_key)
    prefix = f"backer-files-live-test/{uuid4().hex}"
    settings = {
        "bucket": bucket, "prefix": prefix, "endpoint": endpoint, "region": "us-east-1",
        "access_key_id": access_key, "secret_access_key": secret_key,
    }
    transport = S3Sidecar(settings, settings)
    backend = S3FilesBackend({"s3": settings, "repository_id": uuid4().hex, "job_name": "daily"})
    repository = BackupDestination(f"s3://{bucket}/{prefix}")
    destination = BackupDestination(repository.path + "/Agents/daily")
    source = tmp_path / "source"
    source.mkdir()
    payload = bytes(range(256)) * 8192
    filename = "large space + café.bin"
    (source / filename).write_bytes(payload)
    (source / "deleted.txt").write_text("first snapshot only")
    try:
        result = backend.init_repo(repository)
        assert result.success, result.errors
        assert backend.repository_probe(repository.path) == ("present", backend.config["repository_id"])
        first = backend.backup(BackupSource(source), destination)
        assert first.success, first.errors
        first_id = first.metadata["snapshot_id"]
        object_key = f"Agents/daily/snapshots/{first_id}/contents/{filename}"
        assert transport.get(object_key) == payload
        with pytest.raises(RuntimeError):
            transport.upload_if_absent(object_key, source / filename)
        with pytest.raises(RuntimeError):
            transport.put_if_absent(".backer/repository.json", b"must not overwrite")
        assert backend.repository_probe(repository.path)[0] == "present"

        (source / filename).write_bytes(b"second")
        (source / "deleted.txt").unlink()
        second = backend.backup(BackupSource(source), destination)
        assert second.success, second.errors
        assert len(backend.list_snapshots(destination)) == 2
        restored = tmp_path / "restored"
        result = backend.restore(destination, restored, snapshot=first_id)
        assert result.success, result.errors
        assert (restored / filename).read_bytes() == payload
        assert (restored / "deleted.txt").read_text() == "first snapshot only"
        result = backend.restore(destination, tmp_path / "latest", snapshot=second.metadata["snapshot_id"])
        assert result.success, result.errors
        assert (tmp_path / "latest" / filename).read_bytes() == b"second"
        assert not (tmp_path / "latest" / "deleted.txt").exists()
        checked = backend.check(destination)
        assert checked.success, checked.errors
        preview = backend.prune(destination, keep_last=1, dry_run=True, source_path=str(source))
        assert preview.success, preview.errors
        assert len(backend.list_snapshots(destination)) == 2
        pruned = backend.prune(destination, keep_last=1, source_path=str(source))
        assert pruned.success, pruned.errors
        assert len(backend.list_snapshots(destination)) == 1
        checked = backend.check(destination)
        assert checked.success, checked.errors
    finally:
        # Never use repository wipe: the test owns exactly this randomly allocated prefix.
        scope = prefix + "/"
        for key in transport.list(""):
            assert key.startswith(scope), "Refusing to clean up an object outside this test prefix"
            transport.delete(key[len(scope):])
