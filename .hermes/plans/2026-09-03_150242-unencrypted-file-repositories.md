# Plan: encrypted Kopia and unencrypted file repositories

## Goal

Add an explicit repository storage format while keeping the current behavior as the safe default:

- `kopia`: encrypted, versioned Kopia repository; default for every existing or unspecified repository.
- `files`: unencrypted, browsable, immutable full-copy snapshots using platform filesystem APIs.

Repository format is independent of transport (`local`, `smb`, `s3`) and belongs to the repository, not to a job. Jobs continue to reference only a repository ID/name.

## Decisions and scope

- Use `format: Literal["kopia", "files"] = "kopia"` in local YAML and the equivalent server repository record. Never infer format from a missing passphrase.
- Make format immutable after repository creation. Attaching checks the on-disk format marker and fails on mismatch or unknown versions.
- Implement one Python stdlib files backend for both Windows and Linux. Do not restore the removed rsync/rclone/restic engines and do not add native-command adapters or C# copy code.
- Keep the Avalonia app as a CLI front end. Keep Android's existing plaintext tar.gz proxy upload/download protocol; the server chooses the repository implementation.
- Files v1 supports direct `local` and mounted/UNC `smb`, plus server-local proxy storage used by Android. Reject `files` + `s3` explicitly. NFS and object-storage support can follow separately.
- Use immutable full snapshots, not a synchronized `current` mirror: source deletions must not delete old backups and retention must have real generations to manage.
- An encrypted proxy backup must not retain the current plaintext `Agents/<job>/contents` mirror after Kopia accepts its snapshot. Extract into temporary staging, snapshot with Kopia, then remove staging.
- Plain files mode cannot promise Kopia deduplication/compression or complete filesystem fidelity. V1 preserves regular-file contents and basic timestamps/mode where supported; it rejects symlinks and reports unreadable/open files as a failed snapshot. VSS, ACLs, ADS, xattrs, sparse files, hard-link identity, and crash-consistent live databases are out of scope.

## On-disk files format

Create a small versioned marker when initializing a files repository, for example `.backer/repository.json` containing the repository ID, `format: "files"`, and `format_version: 1`.

Store each completed snapshot under:

```text
Agents/<safe-job>/snapshots/<snapshot-id>/
  contents/...
  manifest.json
```

Build under a sibling `.partial-<random>` directory, copy and hash every regular file, write `manifest.json` last, fsync where the platform permits, then commit with a same-filesystem atomic rename. A partial/error/cancelled run never becomes listable. Snapshot IDs come from the existing run ID and are treated as opaque identifiers.

The manifest records format version, job identity, source path, timestamps, file count/bytes, and per-file SHA-256/size. Snapshot listing trusts only completed manifests under the expected job root. All joins use normalized relative paths and containment checks; no operation follows repository or destination symlinks.

## Implementation phases

### 1. Add the repository-format contract and compatibility defaults

- Add `format` to `src/backer/core/config.py::RepositoryConfig`, defaulting to `kopia`; retain strict rejection of job-level `backend`, `backend_type`, and `backend_options`.
- Mirror it in `desktop/Backer.Desktop/Services/ConfigModels.cs`.
- Persist it in server repository records through `src/backer/server/storage.py`. Prefer the existing repository `config` JSON to avoid a schema migration if all repository read/write paths already preserve it; otherwise add one nullable/defaulted column with a startup migration. Existing rows read as `kopia` and are not rewritten merely by reading.
- Add `format` to repository API/web serialization and to `.backer` repository metadata/sidecars where repository identity is written. Legacy metadata without it means `kopia`.
- Add a Python-agent capability such as `files-repository-v1` to registration/heartbeat and persist it. Refuse to queue a direct files-mode job to an older agent; an ignored field must never cause that agent to initialize Kopia in a files repository. Android remains eligible because it always uses proxy transport and the server performs storage dispatch.
- Reject unknown formats everywhere at validation boundaries.

Likely files: `src/backer/core/config.py`, `src/backer/core/repo_metadata.py`, `src/backer/server/storage.py`, `src/backer/server/models.py`, `src/backer/server/app.py`, `src/backer/agent/service.py`, `src/backer/client/agent.py`, Android API models only if the capability is included in Android heartbeats, and the C# config mirror.

### 2. Implement the direct Windows/Linux files backend

- Add `FILES` to `BackendType` and register a single `FilesBackend` in `src/backer/backends/registry.py`.
- Implement initialize/probe, backup, normalized snapshot listing, restore, integrity check, and prune in `src/backer/backends/files.py` using `pathlib`, `os`, `shutil`, `hashlib`, and `json` only.
- Reuse `BackendBase`, `BackendResult`, `BackupSource`, `BackupDestination`, current progress callbacks, mount contexts, job subfolder normalization, and metadata writers.
- Select the backend from trusted repository format passed in `repository_options`; URI shape remains transport selection only (`proxy` still means proxy). Update `src/backer/core/runner.py`, `src/backer/core/destination.py`, and `src/backer/serverless/runs.py` so mounted SMB works for both formats.
- Reject repository/source overlap, repository/restore overlap, filesystem roots, symlinks, non-regular source entries, manifest traversal, and snapshot/job ownership mismatch before copying.
- Apply existing includes/excludes consistently. Any unreadable/stat/copy/hash error fails the snapshot and leaves only removable partial staging.
- Make commits idempotent by snapshot/run ID because scheduler paths can retry or duplicate queueing; an identical completed ID is success, while conflicting content under the same ID fails closed.
- Keep the public snapshot JSON shape used by desktop restore: `id`, `full_id`, `timestamp`, `paths`, and `size`.

Likely tests: `tests/test_backends.py`, a focused `tests/test_files_backend.py`, `tests/test_core_runner.py`, `tests/test_serverless.py`, `tests/test_serverless_e2e.py`, and SMB path tests in `tests/test_smb_browse.py` or `tests/test_core_runner.py`.

### 3. Make local CLI repository lifecycle format-aware

- Add `--format [kopia|files]` to `backer repo add`, default `kopia`. For files mode, forbid passphrase flags, recovery export, S3, and ambiguous non-empty destinations; initialize or attach only through the versioned marker.
- Branch shared repository helpers in `src/backer/serverless/repositories.py`: Kopia keeps current passphrase/probe/create/maintenance-owner behavior; files mode never creates or reads a repository-passphrase secret.
- Update `_repository_backend`, `repo test`, `snapshots`, `verify`, restore, and prune in `src/backer/cli.py` to dispatch by repository format while preserving command/output contracts.
- Update `src/backer/serverless/retention.py` to use the selected backend. Preview first, re-list immediately before apply, require the exact candidate ID set to match, and delete only validated completed snapshots beneath that job's snapshot root. Never delete unknown, foreign, partial, symlinked, or last viable snapshots. A partial deletion is an error with exact survivors reported.
- Harden MERGE restore in `src/backer/cli.py` to reject a symlink or non-directory destination before the files backend expands restore coverage. Preserve the existing NEW/REPLACE protected-root, dry-run, typed-confirmation, staging, and rollback gates.

Likely tests: `tests/test_cli_serverless.py`, `tests/test_serverless.py`, `tests/test_serverless_unattended.py`, `tests/test_config_contract.py`, `tests/test_config_unification.py`, and `tests/test_repo_metadata.py`.

### 4. Update the Avalonia repository wizard without adding backup logic

- Add an encrypted/unencrypted choice before storage details in `RepositoryViewModel.cs` and `RepositoryView.axaml`; select encrypted/Kopia by default and explicitly warn that files-mode contents and filenames are readable by anyone with storage access.
- Emit `--format files` only for the non-default choice. Encrypted creation keeps the current passphrase step; unencrypted creation skips passphrase generation, confirmation, reveal, and recovery export.
- Gate passphrase export/recovery actions and removal warnings in `SettingsViewModel.cs` / `SettingsView.axaml` by repository format.
- Keep job create/edit/run and restore CLI commands unchanged. Make `RunViewModel.cs` progress wording backend-neutral.
- Extend wizard, settings, config mirror, and run-pipeline tests; do not add C# filesystem-copy dependencies.

Likely files: `desktop/Backer.Desktop/ViewModels/RepositoryViewModel.cs`, `Views/RepositoryView.axaml`, `Services/ConfigModels.cs`, `ViewModels/SettingsViewModel.cs`, `Views/SettingsView.axaml`, `ViewModels/RunViewModel.cs`, and matching files under `desktop/Backer.Desktop.Tests/`.

### 5. Dispatch managed/server and Android proxy operations by repository format

- Make server repository creation accept `format`, default `kopia`, and conditionally require/generate/store a Kopia password. Files mode creates/checks only its marker and never stores a repository password. Format validation occurs before any directory creation or write.
- Add format to the one shared backup payload builder and every restore payload builder. Direct Windows/Linux agents receive format plus the capability gate; Android continues to receive `backend=proxy` with the same signed capability.
- Branch `/api/repo/{repo_id}/init`, `check`, `snapshots`, `backup`, `restore`, `prune`, and `check-integrity` from the stored repository record, never request input. Keep authorization and job-scoped capability checks common.
- For files proxy backup, safely extract the Android/Python tar into snapshot staging, reject unsafe/symlink/archive entries, build the same manifest, and atomically commit it. For restore, validate/hash the selected immutable snapshot before creating and streaming a tar.gz.
- Replace `ProxyBackend`'s unfiltered `tar.extractall` restore with the shared safe extraction rules, and forward its existing `include_path` argument as the server's `include` query parameter; both are required before writable plaintext snapshots broaden exposure.
- For Kopia proxy backup, stop publishing a persistent plaintext `contents` tree: snapshot temporary extracted contents and clean them after a successful Kopia commit. Return non-2xx when proxy backup fails; currently Android treats a `200 {success:false}` response as success.
- Make managed retention delete physical snapshots through the selected repository backend. The current `RetentionManager` removes DB history only and must not be presented as storage retention.
- Branch scan/import, repository tests, password endpoints, storage usage, and UI actions so passphrase operations are unavailable for files mode and legacy repositories remain Kopia.

Likely files: `src/backer/server/app.py`, `src/backer/server/storage.py`, `src/backer/server/retention.py`, `src/backer/server/web/routes.py`, `src/backer/server/web/templates/repositories.html`, `src/backer/server/web/templates/jobs_new.html`, `src/backer/backends/proxy.py`, and server/protocol tests.

### 6. Fix Android storage access, keeping its backup protocol stable

- Keep `BackerApiService`, `TarArchiveCreator`, `TarArchiveExtractor`, `BackupWorker`, and `RestoreWorker` repository-format blind; the tar stream is unencrypted transport data for either server-side format.
- Add a clear all-files-access onboarding/status guard for unattended user-file backup (`MANAGE_EXTERNAL_STORAGE`) and fail jobs before scanning/restoring when access is absent. Android Auto Backup cannot back arbitrary user files, and Storage Access Framework would require persisted URI mappings and a broad URI-based worker/archive refactor.
- Document that Android still cannot access other apps' protected data and that Android 15 data-sync foreground work has time limits; large jobs must fail visibly and be retryable.
- Preserve the extractor's traversal/symlink/atomic clean-restore protections. Add tests for permission denial, partial archive creation, server-declared upload failure, snapshot ID propagation, and existing clean-restore rollback.

Likely files: `android/app/src/main/AndroidManifest.xml`, `MainActivity.kt` or the existing settings/status UI, `data/repository/FileBrowserRepository.kt`, worker tests, and possibly `BackupWorker.kt` only for response/snapshot handling.

### 7. Documentation, compatibility, and release validation

- Update `README.md`, `serverless-backups.md`, `desktop/README.md`, and `android/README.md` with the format/transport matrix, plaintext warning, unsupported metadata, Android access limits, restore semantics, and disaster-recovery layout.
- State explicitly that no in-place conversion exists. Users create a new repository of the desired format and run a fresh backup.
- Keep Kopia as the only packaged external backup binary; files mode adds no dependency or installer payload.
- Treat `protocolfixes.md`, the removal changelog, and deleted engine code as history; do not revive old engine selectors or `rsync --delete` behavior.

## Verification

Minimum focused cases before full suites:

- Missing format defaults to Kopia across YAML, SQLite/API, C# config, commands, and sidecars; explicit files round-trips; unknown/mismatched markers fail without writes.
- Encrypted creation still requires and stores a passphrase; files creation cannot accept/store/export one; S3 + files is rejected.
- Files backup covers first run, changed/deleted source, excludes, cancellation, unreadable files, symlinks, overlap, atomic failure, and orphan partials. Old snapshots remain unchanged.
- Files restore covers exact/latest IDs, wrong-job IDs, include traversal, corrupt/hash-mismatched manifests, dry-run, NEW/MERGE/REPLACE, cancellation, empty output, and rollback before destination mutation.
- Retention covers no policy, preview, revalidation race, containment, symlinks, foreign/partial directories, last viable snapshot, partial deletion, and real managed/server deletion.
- Proxy tests cover both formats, auth/capability scope, unsafe tar entries, atomic commit/rollback, error HTTP status, Android upload/download, and encrypted mode leaving no persistent plaintext mirror.
- Run: `./.venv/Scripts/python.exe -m pytest -q tests/`
- Run: `ruff check src/ tests/`
- Run: `dotnet test desktop/Backer.Desktop.sln`
- Run Android unit tests with the repository's Gradle wrapper.
- Manually test one Windows local, one Windows SMB, one Linux local, one Linux SMB, and one Android-to-server-local files repository: create, two backups with a deletion/change, list, restore NEW and REPLACE, verify, retention preview/apply, and recovery after restarting the app/server.

## Risks and open choices

- **At-rest encryption gap:** current proxy Kopia backups keep a plaintext live `contents` tree beside the encrypted snapshots. This plan removes it so “encrypted” is accurate; any feature relying on that live mirror must switch to snapshot restore/browse.
- **Storage cost:** files mode stores complete copies and hashes, with no deduplication. This is deliberate for a first simple, inspectable implementation.
- **Android permission choice:** `MANAGE_EXTERNAL_STORAGE` best preserves unattended raw-path jobs but requires special user consent and Play policy eligibility. Choosing SAF instead materially expands the Android design and requires local URI mapping.
- **Dirty worktree:** this branch already contains broad uncommitted changes, including restore/retention safety work. Implementation must preserve and build on them rather than overwrite or revert them.
