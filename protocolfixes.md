# Protocol and backend remediation plan

## Goal

Make the advertised backend matrix true for every supported agent/runtime, and
make backup/restore safe before expanding storage-provider support.

## Support contract to adopt first

Until the work below is complete, publish this narrower contract:

| Backend | Supported destinations | Notes |
| --- | --- | --- |
| rclone | local paths, mounted SMB/NFS | Mirror only; no history/version restore. |
| restic | local paths, mounted SMB/NFS | Encrypted, versioned repository. |
| kopia | local paths, mounted SMB/NFS | Encrypted, versioned repository. |
| rsync | not agent-supported | Remove all agent-facing selection paths. |

Do not advertise S3 or "50+ cloud providers" until there is a credential
model, provider configuration, connection test, backup, list, restore, and
integration coverage for each advertised provider. Do not describe a local
repository as rclone/restic-backed: it currently uses proxy + server Kopia.

## Phase 0 — stop unsafe/broken flows

1. Disable the affected UI/API actions behind a temporary capability matrix:
   - hide rsync from job editing and reject it at job creation/API validation;
   - disable S3/cloud repository creation and remove cloud support claims;
   - label local repositories as `proxy/kopia`, or disallow selecting a
     different backend for them;
   - disable historical-run selection for rclone restores; and
   - reject Kopia restore dry runs until a non-mutating implementation exists.
2. Reject `clean_restore` unless repository and selected snapshot have been
   successfully opened and validated. Never clear a target before that point.
3. Stop logging decrypted command payloads and passwords. Remove passwords
   from command-line arguments where an auth-file/environment option exists.
4. Prevent cross-agent proxy access now: require a signed, short-lived job/run
   capability on proxy backup, list, restore, prune, and snapshot endpoints;
   verify its repo, job, client, permitted subfolder, operation, and expiry.

Acceptance:

- Invalid/unsupported backend and repository combinations return 4xx before a
  job is saved or queued.
- A valid agent cannot read, write, list, restore, or prune another job's repo.
- Dry-run and failed clean restores leave destination contents unchanged.
- Logs, process lists, and API job responses contain no repository secrets.

## Phase 1 — repair repository and credential protocol

1. Separate secrets in the data model and payload:
   - `storage_password` for SMB transport authentication;
   - `repository_password` for Restic/Kopia encryption;
   - provider credentials only in a provider-specific encrypted config.
   Never copy one value into the other field. Migrate existing
   `restic_password` records deliberately and encrypt them at rest.
2. Define a typed `BackendCapabilities` matrix used by job validation, web
   forms, payload building, maintenance APIs, and both agent runtimes.
   Include: supported repository types, snapshots, partial restore, dry-run,
   init, prune, check, and history semantics.
3. Normalize repository config at the storage boundary. Keep it a dict after
   deserialization; remove the second `json.loads()` calls in init/prune/check.
   Persist an explicit `backend_type` at repository creation.
4. Make maintenance match the actual repository:
   - local proxy repos target ServerKopia's `.kopia-repo` configuration;
   - Restic/Kopia receive only supported method arguments;
   - rclone and rsync return an explicit unsupported-operation response for
     init/prune rather than failing via missing methods; and
   - define integrity precisely: repository validation for Restic/Kopia,
     accessibility-only for rclone, clearly labelled as such.
5. Make proxy uploads snapshot an atomic staging tree. Extract and validate the
   archive in a new directory, replace the job's live contents atomically, then
   snapshot it. This must remove files deleted at the source.

Acceptance:

- Different SMB and repository passwords work for backup and restore.
- Init/list/prune/check work for each capability-supported combination.
- Two proxy runs where the second deletes a file yield a latest snapshot
  without that file.

## Phase 2 — one agent execution protocol

1. Choose one implementation as the execution authority. The practical path
   is to move platform/mount helpers into shared code and have GUI/service use
   `BackerAgent` plus that shared executor; delete duplicate per-backend
   runners once behavior is identical.
2. Build a true unattended Windows service entry point. Do not schedule the
   Tk GUI executable; package a dedicated service/CLI runner and verify it
   starts without an interactive desktop.
3. Make backend readiness lazy and job-specific. A rclone-only job must not
   fail because Kopia cannot download.
4. Apply SMB/NFS preparation consistently to backup and restore on Linux and
   Windows, with credentials passed through the selected executor. Explicitly
   reject unsupported Windows NFS until it is implemented.
5. Standardize operation semantics across runtimes:
   - callbacks report usable progress or are omitted from the contract;
   - retries are owned by one shared layer;
   - Restic/Kopia partial restore passes an actual snapshot/path selector;
   - rclone restore communicates that it restores only current state; and
   - all dry-run behavior is non-mutating.

Acceptance:

- CLI/Linux, GUI/Windows, and installed Windows service run the same backup and
  restore scenario for every supported backend/destination combination.
- Protected SMB backup and restore work from a fresh Windows session.
- The service starts at boot/lock screen and completes a queued rclone-only job
  with Restic/Kopia absent.

## Phase 3 — make tool delivery reliable and safe

1. Fix Windows Restic metadata and extraction to use its ZIP asset; cover every
   supported OS/architecture URL with release-asset tests.
2. Bundle Kopia with the Windows installer or install it into a writable
   per-user/program-data tools directory before startup. Do not require it for
   unrelated jobs.
3. Include Kopia in `backer setup`, make failures non-zero, and correct the
   installer invocation so it does not pass unsupported CLI options.
4. Pin current non-vulnerable tool releases. Download only after checksum and
   signature verification; never disable TLS certificate/hostname verification.
5. Align Docker image, installer, CLI, and documentation on whether tools are
   bundled or downloaded on demand. Correct Kopia version probing to use
   `kopia --version`.

Acceptance:

- Fresh Linux, Windows GUI, Windows service, and Docker installs can obtain the
  selected tool without elevated interactive repair.
- Bad checksum, invalid TLS, missing asset, and extraction failure fail closed
  and produce a non-zero installer/setup result.

## Phase 4 — cloud providers (separate release)

Do this only after phases 0–3. Start with one provider, S3, rather than a
generic "50+ providers" promise.

1. Add encrypted provider credentials and endpoint/region/path-style settings
   to repository storage; expose only required fields in the UI/API.
2. Implement S3 connection testing, payload construction, backup, snapshot
   listing, restore, maintenance, and diagnostics for each backend that claims
   S3 support. Decide explicitly whether rclone uses managed remotes or
   on-the-fly configuration.
3. Add one end-to-end S3-compatible test environment and document the exact
   support matrix. Repeat provider-by-provider; do not infer support from the
   underlying tool alone.

Acceptance:

- A clean agent can configure, test, back up, list, restore, and prune an S3
  repository without pre-existing local rclone/Kopia configuration.

## Test plan and release gate

Add small real-tool integration tests (temporary local dirs) for rclone,
Restic, and Kopia: first backup, changed/deleted-file backup, list, restore,
clean restore failure safety, dry run, prune/check where supported. Add mocked
contract tests only for cloud/network boundary errors.

Add CI matrices for:

- tool URL/checksum/extraction on Linux and Windows;
- job/repository capability validation;
- proxy authorization and secret redaction;
- payload password separation;
- local proxy deletion semantics; and
- Windows service startup smoke test.

Release only when every advertised matrix cell has a passing end-to-end test;
otherwise remove that cell from UI and documentation.
