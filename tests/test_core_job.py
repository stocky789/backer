from datetime import datetime
from pathlib import Path

from backer.backends.base import BackendResult, OperationType
from backer.core.config import DestinationConfig, JobConfig, SourceConfig
from backer.core.job import BackupJob
import backer.core.job as job_module


def test_backup_job_uses_kopia_without_removed_backend_options(monkeypatch) -> None:
    class Backend:
        def check_available(self):
            return True, "ready"

        def backup(self, **_kwargs):
            return BackendResult(True, OperationType.BACKUP, datetime.now(), datetime.now())

        def restore(self, **_kwargs):
            return BackendResult(True, OperationType.RESTORE, datetime.now(), datetime.now())

        def list_snapshots(self, _destination):
            return [{"id": "snapshot"}]

    backend = Backend()
    monkeypatch.setattr(job_module, "get_backend", lambda name, options: backend)
    monkeypatch.setattr(BackupJob, "_save_run_history", lambda *_: None)
    job = BackupJob(JobConfig(name="photos", source=SourceConfig(path="/photos"), destination=DestinationConfig(path="/repo")))

    assert job.run().status.value == "success"
    assert job.restore(Path("/restore")).success
    assert job.list_snapshots() == [{"id": "snapshot"}]
