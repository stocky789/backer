"""Exercise the plaintext S3 lifecycle and failed writes without remote credentials."""

from backer.backends.base import BackupDestination, BackupSource
from backer.backends.s3_files import S3FilesBackend


def test_s3_files_roundtrip_and_fail_closed_retention(tmp_path, monkeypatch):
    objects = {}
    writes = []

    class Store:
        def __init__(self, settings, credentials):
            self.prefix = settings["prefix"]

        def _key(self, key):
            return self.prefix + "/" + key.strip("/")

        def list(self, prefix):
            return [key for key in objects if key.startswith(self._key(prefix))]

        def get(self, key):
            return objects.get(self._key(key))

        def put_if_absent(self, key, data):
            full = self._key(key)
            if full in objects:
                raise RuntimeError("Precondition failed")
            objects[full] = data
            writes.append(full)

        def upload_if_absent(self, key, path):
            self.put_if_absent(key, path.read_bytes())

        def download(self, key, path):
            with path.open("xb") as output:
                output.write(objects[self._key(key)])

        def delete(self, key):
            objects.pop(self._key(key), None)

    monkeypatch.setattr("backer.backends.s3_files.S3Sidecar", Store)
    backend = S3FilesBackend({"s3": {"bucket": "bucket", "prefix": "repo"}, "job_name": "job"})
    root = BackupDestination("s3://bucket/repo")
    destination = BackupDestination(root.path + "/Agents/job")
    source = tmp_path / "source"
    source.mkdir()
    (source / "hello.txt").write_text("plain content")
    (source / "nested").mkdir()
    (source / "nested" / "manifest.json").write_text("ordinary source data")
    assert backend.init_repo(root).success
    assert backend.repository_probe(root.path)[0] == "present"
    initial = dict(objects)
    assert backend.backup(BackupSource(source), destination, dry_run=True).success
    assert objects == initial
    first = backend.backup(BackupSource(source), destination)
    assert first.success, first.errors
    assert writes[-1].endswith("/manifest.json")
    assert b"plain content" in objects.values()
    (source / "hello.txt").write_text("new content")
    second = backend.backup(BackupSource(source), destination)
    assert second.success, second.errors
    assert len(backend.list_snapshots(destination)) == 2
    restored = tmp_path / "restored"
    assert backend.restore(destination, restored, first.metadata["snapshot_id"]).success
    assert (restored / "hello.txt").read_text() == "plain content"
    assert (restored / "nested" / "manifest.json").read_text() == "ordinary source data"
    assert backend.check(destination).success
    newest_key = next(key for key in objects if second.metadata["snapshot_id"] in key and key.endswith("hello.txt"))
    original = objects[newest_key]
    objects[newest_key] = b"corrupted"
    before = dict(objects)
    assert not backend.restore(destination, restored).success
    assert (restored / "hello.txt").read_text() == "plain content"
    assert not backend.prune(destination, keep_last=1).success
    assert objects == before
    objects[newest_key] = original
    assert backend.prune(destination, keep_last=1).success
    assert len(backend.list_snapshots(destination)) == 1
    before = dict(objects)
    assert backend.prune(destination, keep_last=0).success
    assert objects == before
    for unsafe in ("../escape", "C:/escape", "folder\\escape", "folder//escape"):
        try:
            backend._safe_relative(unsafe)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Accepted unsafe path {unsafe}")

    lock = "repo/.backer/files-locks/job"
    objects[lock] = b"another writer"
    before = dict(objects)
    assert not backend.prune(destination, keep_last=1).success
    assert objects == before
    objects.pop(lock)

    # Linux permits names that would become Windows drive/ADS paths during restore.
    import os

    if os.name != "nt":
        (source / "drive:stream").write_text("must not be published")
        before = dict(objects)
        assert not backend.backup(BackupSource(source), destination).success
        assert objects == before
        (source / "drive:stream").unlink()

    def failed_upload(self, key, path):
        raise OSError("upload interrupted")

    monkeypatch.setattr(Store, "upload_if_absent", failed_upload)
    before = backend.list_snapshots(destination)
    assert not backend.backup(BackupSource(source), destination).success
    assert backend.list_snapshots(destination) == before
    assert lock not in objects
