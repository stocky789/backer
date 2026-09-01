"""Serverless retention is an explicit, per-source operation."""

from __future__ import annotations

import json
import re

from backer.backends.base import BackupDestination
from backer.core import keystore
from backer.core.config import BackerConfig
from backer.serverless.repositories import _backend, _destination, repository_operation_context


def prune_job(config: BackerConfig, name: str, *, apply: bool = False):
    job = config.jobs.get(name)
    if not job or not job.retention:
        raise ValueError(f"Job '{name}' has no retention policy configured")
    repository = config.repositories.get(job.repository)
    if not repository:
        raise ValueError(f"Job '{name}' names an unknown repository")
    passphrase = keystore.get(repository.passphrase_ref or "", machine_scope=repository.scope == "machine")
    if not passphrase:
        raise ValueError(f"Repository '{repository.name}' passphrase is unavailable")
    storage = None
    if repository.type in {"s3", "smb"}:
        raw = keystore.get(repository.storage_password_ref or "", machine_scope=repository.scope == "machine")
        if not raw:
            raise ValueError(f"Repository '{repository.name}' storage credential is unavailable")
        storage = json.loads(raw) if repository.type == "s3" else raw
    policy = job.retention
    with repository_operation_context(repository, storage) as operation_record:
        result = _backend(repository, passphrase, storage).prune(
            BackupDestination(_destination(operation_record)),
            keep_last=policy.keep_last,
            keep_daily=policy.keep_daily,
            keep_weekly=policy.keep_weekly,
            keep_monthly=policy.keep_monthly,
            keep_yearly=policy.keep_yearly,
            dry_run=not apply,
            source_path=job.source.path,
        )
    if not result.success:
        raise ValueError("\n".join(result.errors) or "Retention failed")
    match = re.search(r"(?:\b(\d+) snapshot\(s\).*would be deleted|\bDeleted (\d+) snapshots\b)", result.output, re.I)
    return int(match.group(1) or match.group(2)) if match else 0, result.output
