# Serverless backups (direct to repository)

## Goal

Let the Windows and Linux desktop agents define, schedule, run and restore backups against a storage
repository with no Backer server in either plane. The data plane is already direct: the server ships
repository credentials in the command payload (`server/app.py` `_build_backup_command_payload`,
`:551-642`), the agent mounts the share itself (`client/agent.py:733`
`_prepare_destination_for_backend`, `:580` SMB, `:671` NFS) and runs kopia locally, so only `local`
repositories relay through `proxy://`. This feature removes the server from the control plane: job
definitions, credentials, scheduling, run history, retention and progress. It ships as one agent
with a mode switch, not a second product (D5), so one machine holds server-managed and serverless
jobs at once. Android is deferred and stays server-relay-only (D1).

## Repository formats (current)

Repository format is independent of transport and is immutable after creation. `kopia` is the default, including legacy records that omit a format; it supports local directories, SMB/CIFS, and S3-compatible storage with encrypted, compressed, deduplicated snapshots. `files` supports only local directories and mounted/UNC SMB paths. It stores unencrypted, browsable, immutable full-copy snapshots under `Agents/<job>/snapshots/<snapshot-id>/contents/`, with a completed `manifest.json` in each snapshot. S3 is rejected for `files`.

Files repositories expose plaintext to anyone with storage access and do not preserve ACLs, ADS, xattrs, sparse files, hard-link identity, VSS, or crash consistency. Symlinks and unreadable files fail a snapshot. Restore reads one completed immutable snapshot and refuses a destination that overlaps repository storage. Android remains proxy-only: the server selects the format, while Android cannot attach direct local or SMB files storage.

There is no in-place conversion between formats. Create a new repository of the required format and take a fresh backup. Kopia remains the only packaged external backup binary; files mode uses only Python's standard library and adds no dependency or installer payload.

## Decisions

- D1 Android deferred; server-relay-only.
- D2 v1 is kopia to a local directory, an SMB share and S3, on Windows and Linux - six cells, each
  needing a green end-to-end CI job before it is advertised.
- D3 mount and UNC remain the shipping SMB transport for v1, because they work today and add no
  second binary to the data path; kopia's native rclone provider is spiked in parallel and adopted
  in v2 only if it holds up. rclone is not re-added to the tool manager in v1.
- D4 the passphrase is generated, displayed in full, save-confirmed, overridable, and stored in the
  OS keystore only. **Python is the single writer of that keystore.** `core/keystore.py` is the only
  code that calls DPAPI or `secret-tool`; the desktop client never does, because two languages
  writing one secret store is a bug class nothing in this plan would catch. The six-word generator
  and its bundled EFF wordlist stay Python-side for the same reason (A1 item 1): the client shells
  `backer repo add --generate-passphrase` rather than shipping a second copy of a
  security-relevant wordlist. The client learns which backend is in use from
  `backer keystore status --json` (`{"backend": str, "file_fallback": bool}`), a name, never a value.
- D5 one client with a mode switch; config is unified first and the proof is the desktop client's
  install action shelling `backer agent install --mode server`, producing a scheduled task whose run
  leads to a heartbeat; both modes coexist on one machine. "One client" now means one engine and one
  CLI with two front ends; the mode lives in `config.yaml` and in the CLI, not in a UI, which is a
  stronger statement of D5 than a GUI that imported the agent in-process.
- D6 Windows unattended runs as a SYSTEM task with machine-scoped credentials passed explicitly to
  `net use`, including the error-1219 story.
- D7 no new TUI framework: click plus rich, every prompt also a flag, never prompt with no TTY.
  "Every prompt is also a flag" has been promoted from CLI ergonomics to the desktop client's
  integration mechanism (D8): a screen that cannot be expressed as a flagged, non-interactive
  invocation cannot be built. Its corollary: every command the client reads from carries `--json`.
- D8 the desktop client is a C#/Avalonia application at `desktop/` (`Backer.Desktop`, assembly
  `backer-desktop`, net8.0) and reaches the engine in exactly two ways, and no third.
  - **Reads** are direct and read-only: `config.yaml` as B1 defines it, and the files under the data
    directory - `runs/`, `last_attempt/`, `progress/{run_id}.json`, `schedule.json`, `logs/`. The
    client never writes `config.yaml`, never invokes kopia, and never touches the OS keystore (D4).
  - **Every mutation** is a spawned `backer` process. Secrets reach it on stdin only, never on argv,
    which is the rule protocolfixes.md Phase 0 item 3 already sets for the CLI itself. Cancel is
    stopping the child, which is cleaner than the in-process design it replaces: the CLI owns the
    kopia connection and its own `finally` runs the disconnect, so there is no cross-language
    lifecycle registration to invent. The stop is not the same on both platforms and the UI text
    must not pretend it is: on Linux the client sends SIGINT to the child, waits a five-second
    grace for the CLI's own clean stop, and kills the process tree only if it does not go; on
    Windows there is no equivalent signal for a redirected child, so the tree is ended immediately
    and the wording says the run ends now with its snapshot unfinished (`Services/CliRunner.cs`).
  - No local daemon and no HTTP API. A resident local service is refused for the same reasons as
    Not building item 1, and adding one for the GUI would reintroduce it by the back door.
  - User-facing failure text is the CLI's own output, shown verbatim. That is how the
    single-catalogue no-drift guarantee of Appendix A survives a language boundary: there is no
    second catalogue to drift. C# holds chrome strings only - button labels, view titles, column
    headings - and the linting rule that replaces the shared-import claim is that no failure-message
    literal appears under `desktop/`.
  - Cost, stated rather than hidden: a second toolchain in CI and in the installer, and a payload
    that is not a Python module. The gain is a supported UI stack on both platforms, a tray icon on
    Linux that `pystray` could not give (Phase 6 step 9), and a UI that cannot bypass the CLI path
    its own release gate tests.
  - Trigger to reopen: a screen that genuinely cannot be driven by a subprocess. None is known; the
    two that came closest, live progress and scheduler pause, are answered by Phase 6 step 8 and by
    the `backer schedule` group.

## Support contract to adopt

Publish this for v1 and nothing wider.

| Engine or target | Serverless repositories | Status |
| --- | --- | --- |
| kopia | local directory on this machine, SMB share, S3 bucket | v1. Six cells: {Windows, Linux} x {local, SMB, S3}. |
| restic, rclone, rsync | none | Deleted from the product by `704bd8f`. `BackendType` (`backends/base.py:18-19`) and `_BACKENDS` (`backends/registry.py:9-12`) hold KOPIA and PROXY only, so this is not a choice anyone still gets to make. |
| NFS | none | Not in v1: no Windows transport, so it cannot satisfy the both-platforms rule. |
| Android | none | Deferred (D1); server-relay-only, and the README says so (`README.md:55`). |

The standing release rule from protocolfixes.md applies unchanged: release only when every
advertised matrix cell has a passing end-to-end test, otherwise remove that cell from the UI and the
documentation. Six cells means six end-to-end CI jobs, and a cell is advertised in the CLI, the
desktop client and the README only once its job is green. That gate now spans two languages: the
Python `click.Choice` and the client's own type list. `test_cli_choices_match_ci_jobs` (Phase 7)
parses the `Choice` only, so the C# advertisement surface needs its own check; it is named as an
open task in Phase 7 step 5 rather than quietly dropped. Two of the six have foundations already:
`.gitea/workflows/release-validation.yml:49-75` runs the protocol contract on a
`[ubuntu-latest, windows-latest]` matrix, and `:77-114` runs an S3 end-to-end against MinIO
(`tests/test_s3.py:226-234`).

`local` names two different things and the documentation must say so rather than paper over it. A
serverless `local` repository is a directory on this client, written by kopia on this client. A
server `local` repository is a directory on the server, hardwired to the proxy relay:
`_validate_job_config` (`server/app.py:645-659`) admits only `smb`, `nfs`, `local` and `s3`
(`:653`), repository creation applies the same whitelist at `server/app.py:4610-4611`, and
`_build_backup_command_payload` rewrites a `local` destination into a `proxy://` URI carrying a
per-run capability token at `server/app.py:617-629`. A config written for one is not valid for the
other.

## Architecture

This is an extraction, not a greenfield build. `BackerAgent.execute_backup`
(`client/agent.py:864-1075`) and `execute_restore` (`:1214-1493`) are already server-free engines
driven by a plain job dict; their only server coupling is `_report_progress` (`:472-495`) and the
result POST at `:1010-1012`, with a second POST on the error path at `:1060-1064`. Both move to
`backer.core.runner` with `on_progress` and `on_result` injected, so server-managed mode passes HTTP
posters and serverless mode passes local writers. One engine serves both modes.

The twelve clean-restore rollback tests at `tests/test_agent_protocol.py:198-546` do not cover that
extraction for free, and the plan must not claim they do. Four of them now assert the opposite of
their names: `test_clean_restore_keeps_destination_when_validation_fails` (`:198`),
`test_clean_kopia_restore_keeps_destination_when_no_files_match` (`:258`) and
`test_clean_kopia_restore_rolls_back_when_actual_restore_matches_nothing` (`:285`) each assert the
run succeeded and the staged original is gone, and
`test_clean_kopia_restore_uses_one_immutable_snapshot` (`:229`) parametrises two values it never
uses. The product change behind them is `client/agent.py:1285-1293`, where a kopia clean restore is
pre-validated by `list_snapshots` plus a snapshot-ID membership check only; the agent then stages
the destination aside (`:1319-1327`) and deletes the staged original on `result.success`
(`:1396-1398`). A kopia restore that connects, matches nothing and exits zero therefore wipes the
destination and reports green. Phase 0 item 9 removes the wrong-snapshot half of this; the zero-file
half must be fixed and those four assertions restored before `execute_restore` moves anywhere.

```
src/backer/core/
  paths.py        Config, data and run-lock directories; get_job_subfolder from
                  server/repository_paths.py:6-8. One answer per platform, not three.
  mounts.py       SMB/NFS parsing, mount contexts, SMBConnectionManager; from
                  client/agent.py:497-732 and agent/service.py:97-394.
  destination.py  prepare_destination / prepare_source, from client/agent.py:733-862 and :1077-1212.
  runner.py       run_backup / run_restore with on_progress and on_result injected.
  smb_browse.py   Share and directory enumeration on a host the user named; net view plus
                  Path(unc).iterdir() on Windows, SMBBrowser on Linux; server/repositories.py:19-261.
  keystore.py     Windows DPAPI blobs under an ACL'd directory, Linux Secret Service, headless fallback.
  messages.py     Every user-facing string, the Appendix A catalogue. One copy, imported by the
                  CLI; the desktop client displays the CLI's output verbatim rather than importing
                  anything, so there is no second catalogue and nothing to keep in sync (D8).
```

`src/backer/serverless/` holds the GUI-free logic that used to sit inside the Tk package. The whole
module list, because a partial one reads as a smaller package than it is:

```
src/backer/serverless/
  cells.py           PROVEN_SERVERLESS_CELLS and supported_repository_types - the D2
                     advertisement list in one importable place, and the source the
                     desktop client's Cells.cs is contract-tested against.
  modes.py           apply_scheduled_modes - the freeze-verify-rollback install
                     transaction Phase 5 step 1 describes. Rollback is gated on a
                     mutation having happened and restores each file in place.
  schedule.py        The run lock, due-job selection and the paused/until runtime
                     state behind `schedule pause|resume|show|status`.
  retention.py       prune_job: the per-source preview and the explicit apply.
  repositories.py    add_repository, probe, create, the machine-scope re-scoping
                     of secrets, and the recovery record.
  runs.py            run_local_job and run_due_jobs, plus the progress document
                     and per-run log the desktop client reads.
  history.py         Run-history and repository presentation shared by the CLI and
                     the desktop client.
  store.py           append_run / read_runs under the data directory.
  sidecar.py         The repository `.backer/` sidecar: job documents and adoption.
  s3_sidecar.py      The same sidecar over S3, credentials in SigV4 headers only.
  scheduled_test.py  D6's privileged self-test: prepare, wait, remove, retry cleanup.
```

Nothing under `backer.core` or `backer.serverless` imports a UI toolkit.

Where truth lives:

| Data | Authoritative store | Reason |
| --- | --- | --- |
| Jobs, repositories, schedules | The local unified config file | One writer per machine, so there is no multi-writer bug class to design around. |
| Repository passphrase, SMB password, enrolment secret | OS keystore | Never in the config file, never in the repository. |
| Progress and in-flight state | Local file under the data dir | Four laptops writing JSON to a NAS every 200ms is a failure mode, not a feature. |
| Run history and adoption | Repository `.backer/` sidecar | The only store a replacement machine can read. It must be fattened: `save_job` records only `source_path` and `client_id` today (`client/agent.py:1586-1589`), which cannot reconstruct a job. |

A repository record addresses exactly one kopia repository, at the repository root as this machine
reaches it. Every job on that record shares it, which is the point: kopia deduplicates across jobs,
and one passphrase covers the share instead of one per job. Jobs are separated by kopia's own source
identity, `user@host:path`, which `snapshot list` groups and filters on and which
`_find_latest_snapshot_for_source` (`backends/kopia.py:386-449`) already reads. Retention is scoped
the same way, per source, so a laptop's snapshots cannot be evicted by a desktop that backs up
hourly - the per-machine property falls out of the host component for free. There is no snapshot
tagging scheme. `get_job_subfolder` names sidecar directories only - `jobs/{job_subfolder}/` -
byte-identical to what `server/app.py:575` builds so the server's importer reads the job document
unchanged; it never names a kopia repository.

Server-managed mode does not use this layout for every repository type, and the split has to be
stated once. S3 already matches it: `server/app.py:637` points every job at
`s3://{bucket}/{prefix}` with no subfolder. SMB and NFS do not: `server/app.py:600` and `:612`
append `Agents/{job_subfolder}`, giving each job its own kopia repository under the same storage
root. Those legacy per-job trees are read for discovery and never written by the serverless path. A
server therefore cannot adopt a serverless repository's backup data as-is, only its job records.

### Repository connection model

Kopia is connection-oriented where restic was stateless, and every later phase depends on this being
stated once. `kopia repository connect <provider> ...` writes a config file recording provider,
credentials and repository identity, and every subsequent command operates on "the currently
connected repository" - none of them names a repository. The command builders show it:
`["snapshot", "create", "--json", ...]` (`backends/kopia.py:281-311`),
`["snapshot", "list", "--json", "--all"]` (`:596`), `["policy", "set", "--global", ...]` (`:664`).
There is no `--repo` escape hatch.

The backend therefore wraps every operation in connect - do - disconnect, with the disconnect in a
`finally`: `backup` connects at `:253` and disconnects at `:383-384`, `restore` at `:487` and
`:581-582`, `list_snapshots` at `:590` and `:618-619`, `prune` at `:651` and `:746-747`, `check` at
`:757` and `:786-787`, `get_snapshot_files` at `:809` and `:829-830`. Three consequences a
serverless agent has to own explicitly:

1. The connection is deliberately not persisted between runs, so the agent pays kopia's
   repository-open cost on every operation. Over a UNC path or a cold S3 endpoint that is
   measurable per-run overhead, and it is the reason the local scheduler must not fan out one kopia
   process per job.
2. State that the code does not manage does persist. The content cache lives outside the config
   file (`KOPIA_CACHE_DIRECTORY`) and survives disconnect, and `--persist-credentials` defaults on
   in kopia 0.23.1 while the backend passes neither that flag nor `--no-persist-credentials`, so on
   Windows connecting may write the passphrase into Credential Manager as a side effect. D4 says the
   keystore is the only home for the passphrase, so the decision is explicit: pass
   `--no-persist-credentials` on every connect and keep the OS keystore the single writer. Phase 0
   item 5 owns the config and cache paths that make this enforceable.
3. The disconnect is global until item 5 lands, so one job's `finally` tears down another job's live
   connection. Making connect failures legible is item 6, and until both land no connect-time error
   message on this path can be trusted.

Credentials also enter the environment as a side effect of connecting: `_get_repo_type`
(`backends/kopia.py:98-104`) is what puts `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` into
`self._env`, and it is reached only through `init_repo` and `_connect_repo`. Any fast path that
skips the connect silently loses the S3 credentials.

### Sidecar safety

The sidecar is made safe by atomic writes and single-writer-by-naming, not by locking. The defect,
stated once here: `RepositoryMetadata._write_json` (`core/repo_metadata.py:249-259`) wraps its write
in `file_lock(path, mode="w")` at `:254`, and `file_lock` opens the file at `:70` before acquiring
the lock at `:73-87`, so the truncate always precedes the lock and the lock has never protected the
write it wraps. The Windows branch does take a real lock via `msvcrt.locking` at `:77-83`; the
defect is the ordering, not a missing lock. Every sidecar file is instead named by its single writer
- `agents/{agent_id}.json`, `jobs/{job_subfolder}/runs/{run_id}.json`, content-addressed snapshot
records - so nothing is contended. `file_lock` stays in the module regardless: `server/app.py:27`
imports it, `:83` and `:12871` use it on genuinely local paths, and
`tests/test_server_regressions.py:171` monkeypatches it by name.

### Concurrency

There is no cross-machine lease, and the document does not imply one. Use a local `flock`/`msvcrt`
lock on `<data_dir>/run.lock`, which is correct because the data dir is always a local filesystem,
plus run_id namespacing inside the sidecar. The operations that genuinely must not overlap are
`snapshot expire --delete` and `maintenance run --full` (`backends/kopia.py:696`, `:711`), so
retention runs behind the same local lock as backup, and the per-repository mutex Phase 0 item 5
ports from `server/app.py:78-86` covers the same-machine case. Two machines pruning one repository
at the same moment get kopia's own behaviour, and the documentation says that plainly rather than
promising a guarantee Backer does not implement.

## SMB transport (D3 resolved)

Kopia has no SMB provider of its own. Verified against the pinned 0.23.1 binary
(`tools/manager.py:21-37`), `kopia repository create` offers exactly `azure, b2, filesystem,
from-config, gcs, gdrive, rclone, s3, sftp, webdav`. An SMB destination therefore reaches kopia as a
filesystem path: `_get_repo_type` (`backends/kopia.py:129-133`) falls through to
`("filesystem", ["--path", path])` for anything without a recognised scheme, and the agent is
already written for that - `client/agent.py:819` says so in one line inside the kopia branch at
`:818`.

The restic-era reasoning for this - "the repository is addressed by path, so an rclone remote can
never be the data path" - is dead, and the conclusion has to be re-derived rather than inherited.
Kopia does have a native rclone provider, `kopia repository create rclone --remote-path=...`,
verified against the pinned binary. It spawns rclone as a subprocess and speaks to it over rclone's
API, so an on-the-fly `:smb,host=...:` remote is a legal kopia data path. The v1 decision is a cost
judgement, not an
impossibility: mount and UNC ship because they work today, are already tested on both platforms, and
add no second binary to the data path, while adopting rclone means re-adding it to `TOOL_INFO`
(`tools/manager.py:20-38`, which holds kopia alone) with its own pin, checksum manifest and per-OS
download, plus a second process between the agent and the share.

The spike runs in parallel and decides v2, not v1. It answers three questions and nothing else: does
a kopia repository created over an rclone SMB remote survive a full backup, prune and restore cycle
on both platforms; does it work unprivileged on Linux where `mount -t cifs` does not; and what does
it cost in throughput against a mounted share on the same hardware. If all three hold, v2 adopts it
and unprivileged Linux stops being a degraded case. If any fails, the spike is deleted and this
section is the record of why.

Transport per platform for v1:

1. Windows: `net use` through `SMBConnectionManager` (`agent/service.py:97-394`), then a UNC path
   handed straight to kopia (`client/agent.py:1152-1155`).
2. Linux as root: `mount -t cifs` via `_smb_mount_context` (`client/agent.py:580`), then the mount
   point.
3. Linux unprivileged: `gio mount smb://server/share` through gvfs, then the gvfs FUSE path under
   `$XDG_RUNTIME_DIR/gvfs`. This is what the file manager does, so it needs no root, no terminal and
   no desktop session - only `gio` and a session bus, which a `systemd --user` timer with linger
   already has.

`smb_mount_context` (`core/mounts.py`) is the single choke point every Linux caller reaches -
`repository_operation_context` (`serverless/repositories.py:106-115`), `prepare_destination` and
`prepare_source` (`core/destination.py`, so backups and restores both), and the metadata write in
`core/runner.py:611` - and it is a four-rung ladder, stopping at the first rung that holds:

1. An existing kernel `cifs` mount of `//server/share` in `/proc/mounts`, matched case-insensitively
   with `/proc/mounts` octal escapes decoded. It is not ours, so it is yielded and never unmounted.
2. `euid == 0`: `mount -t cifs` with the credentials in a `0600` temp file, unmounted in `finally`.
3. Not root, with `gio` and a session bus: the gvfs mount above. Credentials go on the child's stdin
   as three lines - user, domain (empty accepts gvfs's default), password - never on argv. `gio`
   exits 0 after a failed authentication in some versions, so success is the gvfs entry existing
   afterwards, not the exit code; a wrong password therefore surfaces as "Login failed - invalid
   username or password" rather than falling through to another rung that could create a repository
   in the wrong place. The mount is session-scoped and is left mounted on exit, exactly as the file
   manager leaves it.
4. Neither: refuse, leading with the promptless fix (install `gvfs` plus `gvfs-smb` /
   `gvfs-backends`) before pre-mounting, `/etc/fstab`, and running as root.

The honest caveat: gvfs FUSE throughput is below kernel `cifs`, and a root or `/etc/fstab` mount
remains the heavy-duty path for large data sets. The rclone spike stays the v2 item, because it is
what would take FUSE out of the picture entirely.

## Phase 0 - correctness prerequisites

Every defect below is live in server-managed mode too, which is why it lands first and lands once.
No later phase re-fixes any of it; later phases may say they rely on a Phase 0 fix and nothing more.
Item 7 outranks the other nine and ships ahead of them: it is a released regression that leaves user
data unreadable with no repair surface anywhere in the product. Several items are settled by running
the pinned kopia 0.23.1 binary (`tools/manager.py:21-37`) rather than by a unit test; where that is
the honest proof, the acceptance bullet says so.

### Status

Implemented on branch `fix/kopia-retention-and-restore-safety`, five commits, not yet merged.
250 tests pass and `ruff check src/ tests/` is clean; kopia command semantics were settled by running
the pinned 0.23.1 binary, not assumed.

| Item | State | Commit |
| --- | --- | --- |
| 1. `--delete` when expiring | done | `8a90627` |
| 2. Stop emitting `--dry-run` | done | `8a90627` |
| 3. Scope retention to the source | done | `365335d` |
| 4. Replace `check` | done | `8a90627`, `365335d` |
| 5. Per-repository config, cache and mutex | done | `7971ca2` |
| 6. Never create a repository from the backup path | done | `7971ca2` |
| 7. Set the passphrase on an existing repository | done | `8a90627` |
| 8. Fix `get_snapshot_files` | done | `ad23535` |
| 9. Delete the basename fallback | done | `7971ca2` |
| 10a. `_write_json` atomic, `file_lock` off the write path | done | `ad23535` |
| 10b. `discover_all` merges root and per-job sidecars | done | `ad23535` |
| 10c. Run records written on failure | done | `ad23535` |
| 10d. `get_config_dir` XDG branch | done | `core/paths.py` |
| 10e. `get_data_dir` honours `BACKER_DATA_DIR` | done | `core/paths.py` |
| 10f. Desktop UI cannot find `agent.yaml` | **obsolete; subsumed by Phase 1** | - |
| `os.getuid()` guard | already fixed upstream | `24f0768` |

Three defects not in the original list were found reviewing the first attempt at this phase and are
fixed in `365335d`: the clean-restore emptiness guard counted directories as restored content and
skipped entirely for a destination that did not already exist, so a restore producing no files could
delete the staged original and report success; the source scoping in item 3 first landed in a branch
that `_verify_repo_access` makes unreachable, so every request took the unscoped path; and a dry-run
prune persisted the retention policy, arming deletion at the next ordinary snapshot.

Both 10d and 10e are now implemented in `core/paths.py`, and each of the two objections that had
deferred them is answered rather than ignored:

- 10d does not key on whether `/etc/backer` exists - the objection that `backer agent setup` removes
  that directory between two calls to `get_config_dir()`. It keys on `BACKER_CONFIG_DIR`, then on
  `config.yaml` existing under `$XDG_CONFIG_HOME/backer` (`%APPDATA%\Backer` on Windows), then on
  `config.yaml` existing under the machine directory, and falls back to the user directory.
- 10e's data-loss objection - `agent uninstall` `rmtree`ing a colocated server's `BACKER_DATA_DIR` -
  is answered by the `_is_server_data_dir` guard in `cli.py`, which keeps any directory holding
  `backer.db` and says so, for both the data and the config directory. Beyond that, `agent uninstall
  --mode server --service-only` removes only the installed service, task or systemd unit and leaves
  config, data, keystore and binaries untouched; it works with `--yes` and it is what the desktop
  client's "Remove agent service" shells, so the destructive full uninstall is no longer reachable
  from a button.

10f no longer describes a live defect: the
Tk GUI that wrote `%APPDATA%\Backer\config.json` is deleted, and the desktop client reads
`config.yaml` and shells the CLI for every mutation (D8), so nothing writes a private JSON config
any more. What survives is the migration obligation - a machine upgrading from a pre-Avalonia build
still has that file on disk - and that belongs to Phase 1 step 5.

1. Pass `--delete` when expiring snapshots. `prune` builds `["snapshot", "expire", "--all"]`
   (`backends/kopia.py:696`) and never appends `--delete`, so kopia reports what it would remove,
   removes nothing, and `prune` returns `success=True`. The server's own local path gets this right
   at `server/app.py:12705` (`["snapshot", "expire", "--delete"]`), so the asymmetry is in-repo
   evidence rather than inference. Add `--delete` in the same change as item 3 or after it, never
   before: on a repository shared by several jobs, `--delete` under a `--global` policy is exactly
   what turns a silent no-op into cross-job snapshot deletion.

2. Stop emitting a flag that does not exist. `prune` appends `--dry-run` to the expire command
   whenever `dry_run=True` (`backends/kopia.py:697-698`), and `snapshot expire` has no such flag in
   0.23.1 - its only non-global flags are `--all` and `--delete`, and its positional argument is
   `[<path>]`. Kopia exits non-zero on the unknown flag, so `prune(dry_run=True)` always fails while
   `prune(dry_run=False)` always succeeds and does nothing: both wrong, in opposite directions.
   Implement dry run as the expire command without `--delete`, which is kopia's own report-only
   mode, and drop the flag.

3. Scope retention to the job's source instead of the whole repository. The code writes
   `policy set --global` (`backends/kopia.py:664`) and expires `--all` (`:696`). Kopia documents
   `--keep-latest/-hourly/-daily/-weekly/-monthly/-annual` as per source, `policy set` targets
   `user@host:path`, `user@host`, `@host` or `--global`, and `snapshot expire` takes the paths to
   expire. Target the job's source path and expire that path only. That is the whole retention
   design, and it is what replaces the tag scheme a restic-era plan needed.
   - The blast radius is not uniform today and the split is documented nowhere. SMB and NFS get one
     kopia repository per job because `_build_backup_command_payload` appends
     `Agents/{job_subfolder}` (`server/app.py:600` and `:612`), so `--global` is currently harmless
     there. S3 has no subfolder (`server/app.py:637`), so every job on one S3 repository shares it
     and the cross-job deletion hazard goes live the moment item 1 lands.
   - Emit every configured keep flag. `prune` (`backends/kopia.py:626-634`) takes `keep_last`,
     `keep_daily`, `keep_weekly` and `keep_monthly` and emits only those four (`:666-673`), while
     `RetentionConfig` (`core/config.py:24-31`) carries `keep_yearly` at `:31`, so a yearly policy
     is accepted, stored and silently dropped. Kopia spells the flag `--keep-annual`, not
     `--keep-yearly`. Add the parameter and the emission, and return a failure result rather than
     run when the assembled keep-flag list is empty. No phase may expose the flag before this lands.
   - Two jobs on one folder are not separable by source, and the exclude policy is per path
     (`backends/kopia.py`), so two such jobs overwrite each other's ignore rules on every run.
     Forbid that configuration in v1 rather than tag around it. The dead tag hook at
     `backends/kopia.py:307-309` stays dead until v2.

4. Replace `check` with a command kopia has. It runs `repository validate-client`
   (`backends/kopia.py:771`), which is not a 0.23.1 subcommand - `kopia repository` offers
   `connect, create, disconnect, set-client, set-parameters, status, sync-to, throttle,
   change-password, validate-provider`. Every call therefore returns `success=False` with the
   parse error in `output`, so any health surface driven by this shows permanent red. Target
   `snapshot verify --verify-files-percent=N`, the only kopia command that proves content is
   readable rather than merely indexed, and say in the UI that it is materially slower than an index
   check because it downloads and rehashes real content. Fix the caller in the same change:
   `server/app.py:12775` calls `backend.check(dest, dry_run=dry_run)` against a signature taking
   only `destination` (`backends/kopia.py:749`), a `TypeError` swallowed by the blanket handler at
   `server/app.py:12783-12785`.

5. Give every repository its own kopia config, cache and mutex. `KopiaBackend.__init__`
   (`backends/kopia.py:39-48`) sets `KOPIA_PASSWORD` and nothing else, and
   `grep -rn --include='*.py' "KOPIA_CONFIG_PATH" src/` returns exactly one hit,
   `server/app.py:113`, inside `ServerKopia`. Every agent kopia operation therefore mutates the
   user's global kopia config, and every operation ends in `finally: self._disconnect_repo()`
   (`backends/kopia.py:384`, `:581`, `:618`, `:746`, `:786`, `:829`) which tears down a concurrent
   job's live connection - silently, because `_disconnect_repo` swallows every exception
   (`:216-218`). The agent runs one daemon thread per job (`client/agent.py:277-283`) behind a
   de-dup guard keyed only on job name (`:271-275`), so two different jobs at once is the normal
   case, and a local scheduler will not stagger dispatch the way the server does.
   - Set `KOPIA_CONFIG_PATH` and `KOPIA_CACHE_DIRECTORY` per repository, derived per operation from
     `destination.path` under the local data directory (`<data_dir>/kopia/<hash>.config`), never
     inside the repository.
   - Port the mutex that already ships server-side: `_serialized_kopia_operation` wrapping
     `file_lock(<repo>/.backer-kopia.lock)` (`server/app.py:78-86`). Used as a mutex on a separate
     lock file, `file_lock`'s truncate-before-lock ordering defect does not apply.

6. Never create a repository from the backup path. `backup` treats any non-zero connect as
   "repository absent" and creates a new one (`backends/kopia.py:252-277`). A wrong passphrase, an
   unreachable endpoint, an unmounted SMB path and a config collision from a sibling thread all land
   in that one branch. An evaporated mount point yields a fresh empty repository on local disk, a
   successful snapshot and `success=True`, with a `print` to agent stdout (`:256`) as the only
   warning; a wrong passphrase yields "Failed to initialize repository" instead of the single most
   important error message in a serverless product. Probe with `repository status` and distinguish
   absent, unreachable and wrong-passphrase before any create. Delete the auto-init branch and
   return a failed result naming the resolved path and the distinguished cause. Leave `init_repo`
   (`backends/kopia.py:135`) in place with explicit repository creation as its only caller.

7. Ship a way to set the passphrase on an existing repository, and a documented recovery procedure.
   This outranks every other item in this phase. The default-password removal shipped in 0.8.0 with
   no migration: `_build_backup_command_payload` raises when no passphrase is stored
   (`server/app.py:585-587`), repository creation now requires one (`server/app.py:4612` via
   `_repository_password_or_error`, `:545-548`), `Storage.get_repository_password`
   (`server/storage.py:918-924`) reads `repository_password_encrypted` with no legacy fallback, and
   `Storage.set_repository_password` (`server/storage.py:926`) has only test callers - no production
   caller, no endpoint, no UI. A repository created before the passphrase became mandatory can
   neither be opened nor be given a passphrase through any surface the product ships.
   - Add a repository-update path that calls `set_repository_password`, plus the CLI equivalent, so
     an operator who still holds the passphrase can re-attach the repository. There is no
     repository update endpoint at HEAD - `server/app.py:4861` `update_repository_status` is the
     only mutation - so this is new surface, not a wired-up helper.
   - Document recovery in README and record it in CHANGELOG.md. Kopia repositories have one
     passphrase and one re-key command, `repository change-password`; there is no add-then-remove
     safety dance, so the procedure is to confirm the repository opens from a fresh config file
     first, change the passphrase second, and confirm again third. State plainly that a repository
     whose passphrase is genuinely lost is unrecoverable, because that is now true.

8. Fix `get_snapshot_files`, which is wrong twice. It builds
   `snapshot list --json [--path <path>] <snapshot_id>` (`backends/kopia.py:814-817`). In 0.23.1
   `snapshot list` has no `--path` flag, and its positional argument is `[<source>]`, "File or
   directory to show history of" - not a snapshot ID. Listing a snapshot's contents is
   `kopia ls <object-path>`; listing a source's history is `kopia snapshot list <source>`. Pick the
   one the caller needs and pass the argument that command actually accepts.

9. Delete the basename fallback. `_find_latest_snapshot_for_source` falls back to matching on the
   last path component when the exact source path misses (`backends/kopia.py:425-428`), so
   `C:\Users\a\Documents` matches `D:\Archive\Documents`. Serverless restores run unattended with no
   server-side sanity check in front of them, so this is a wrong-data restore waiting for a machine
   with two folders of the same name. Fail loudly instead, naming the source paths that were
   available.

10. Carry forward from the restic-era list, each re-verified at HEAD.
    - `_write_json` is not atomic: it wraps its write in `file_lock(path, mode="w")`
      (`core/repo_metadata.py:254`), which truncates before it locks. Replace the body with a
      same-directory temp file named `<name>.<agent8>.tmp` plus `os.replace`, and drop the
      `file_lock` call from that function only. `save_agent` (`:318-353`) and `save_job`
      (`:377-421`) never used `_write_json`: each holds `file_lock(..., mode="r+")` and rewrites in
      place with `f.seek(0); f.truncate()` (`:414-415`). Convert both to read, merge in memory, then
      write through the same helper - a dropped share mid-write currently leaves a truncated
      `config.json`, the one document adoption depends on.
    - Run records are written only on success. `client/agent.py:1017` wraps the sidecar write in
      `if result.success:` and `:1438` does the same for restores, so a job failing nightly under
      the SYSTEM task writes nothing to the repository and a second agent reading the sidecar sees
      unbroken green. `_write_repo_metadata` also swallows every exception and only prints
      (`:1549-1551`). Remove both guards, add `errors` and `return_code` to the run record at
      `client/agent.py:1593-1602`, and replace the swallow with `logger.error` attached to the
      returned report. The sidecar write stays best-effort and never fails a backup.
    - `discover_all` returns root-level results as soon as `is_initialized()` is true
      (`core/repo_metadata.py:585`) and never reaches the `Agents/*/` scan, so a root sidecar hides
      every server-written per-job sidecar - exactly the coexistence case. Collect the root results,
      always run `scan_folder`, then merge; agents already dedupe by `agent_id` (`:643-648`), so
      dedupe jobs by the `job_name` key written at `:402` and report `initialized` true when either
      source yielded anything. `server/app.py:5251-5496` reimplements the same aggregation over
      `smbclient` and has already diverged; unify it onto the same helper.
    - `get_config_dir()` returns `/etc/backer` on every non-Windows platform with no XDG branch
      (`client/agent.py:75-77`), and `get_data_dir()` has no `BACKER_DATA_DIR` branch
      (`client/agent.py:80-91`) although `tools/manager.py:54` reads that variable and
      `agent/service_entry.py:13-14` sets it. That split is why a scheduled run and the interactive
      user resolve two different data directories, run every job twice, and show each other nothing.
    - Historical, and the reason Phase 1 exists: the deleted Tk GUI wrote
      `%APPDATA%\Backer\config.json` while the agent read `agent.yaml` from the same directory
      (`client/agent.py:114`), so installing the service could not find the config the UI had just
      saved. Same directory, different file, different format. No shipping code writes that JSON any
      more; the file still exists on upgraded machines and Phase 1 step 5 migrates it.
    - `cli.py:655` calls `ag._save_credentials(client_id, client_secret)` against
      `client/agent.py:174` `def _save_credentials(self) -> None:`, raising `TypeError` before
      anything is written. Call it with no arguments; both attributes are already assigned at
      `cli.py:653-654`.
    - `cli.py:224` calls `be.restore(...)` without `original_source_path`. Under kopia the symptom
      is not misplaced files: `KopiaBackend.restore` resolves `snapshot_id = "latest"` when neither
      `snapshot` nor `original_source_path` is given (`backends/kopia.py:502-517`), and `latest` is
      not a valid snapshot reference, so `backer restore` without `--snapshot` fails outright. Pass
      it through from a new `--original-source` option, defaulting to the snapshot's own source.
    - Already fixed, do not re-fix: the unguarded `os.getuid()` at `server/repositories.py:450` now
      reads `os.getuid() if hasattr(os, "getuid") else "N/A"`.

Acceptance:

- `kopia snapshot expire --help` from the pinned binary lists `--delete` and `--all` and no
  `--dry-run`; that output is the proof for items 1 and 2, and
  `tests/test_backends.py::test_prune_expires_with_delete_and_never_emits_dry_run` pins the emitted
  argv for both `dry_run` values.
- Add `tests/test_backends.py::test_prune_scopes_policy_and_expiry_to_the_job_source` (emitted argv
  carries `policy set <source>` and `snapshot expire <source>`, and neither `--global` nor `--all`
  appears) and `::test_prune_emits_every_configured_keep_flag` (`--keep-annual 5` reaches argv from
  `RetentionConfig.keep_yearly`, and a policy whose only field is `keep_yearly` never emits an
  expire without it).
- `kopia repository --help` lists no `validate-client`, and `kopia snapshot verify --help` lists
  `--verify-files-percent`; that pair is the proof for item 4. Add
  `tests/test_backends.py::test_check_runs_snapshot_verify` and
  `tests/test_server_regressions.py::test_repo_check_endpoint_calls_check_with_one_argument`
  (`server/app.py:12775` no longer raises `TypeError` into the blanket handler).
- Add `tests/test_backends.py::test_kopia_sets_per_repository_config_and_cache_paths`
  (`KOPIA_CONFIG_PATH` and `KOPIA_CACHE_DIRECTORY` are both set, differ per destination, and lie
  under neither destination path) and `::test_concurrent_operations_on_one_repository_serialize`
  (two overlapping calls do not interleave connect and disconnect).
- Add `tests/test_backends.py::test_backup_never_creates_a_repository` (against an empty directory
  `backup` returns failure and the directory afterwards holds no kopia blob) and
  `::test_wrong_passphrase_is_reported_as_wrong_passphrase` (a connect failure carrying an
  authentication error returns that cause, not "Failed to initialize repository").
- Add, in `tests/test_repository_credentials.py`,
  `::test_repository_password_can_be_set_on_an_existing_repository` (a repository row with
  `repository_password_encrypted` null is repaired through the new endpoint and
  `get_repository_password` then returns the value), and
  `tests/test_workflow_sanity.py::test_readme_documents_legacy_repository_recovery` (README contains
  the recovery procedure and CHANGELOG.md records it).
- Add `tests/test_backends.py::test_get_snapshot_files_passes_a_source_where_a_source_is_expected`
  (no `--path` in argv) and `::test_snapshot_lookup_never_matches_on_basename` (a repository holding
  only `D:\Archive\Documents` returns no match for `C:\Users\a\Documents` and the error names the
  available sources).
- Add `tests/test_repo_metadata.py::test_write_json_preserves_previous_content_on_failure` and
  `::test_save_job_never_truncates_in_place` (with `json.dump` patched to raise, the file still
  parses to its prior contents and no `*.tmp` remains), plus
  `::test_discover_all_merges_root_and_agent_subfolders` (a repository holding both a root
  `.backer/` tree and an `Agents/<job>/.backer/` tree returns both job names from one
  `discover_all()`). `tests/test_server_regressions.py:171` still monkeypatches `file_lock`, and
  `python -c "from backer.server.app import file_lock"` exits zero.
- Add `tests/test_agent_protocol.py::test_failed_backup_writes_run_record_with_error` and
  `::test_failed_restore_writes_run_record_with_error` (a failing backend produces a run record with
  `status == "failed"`, non-empty `errors` and a `return_code`).
- Add `tests/test_cli_setup.py::test_agent_configure_writes_config` (no `TypeError`) and
  `tests/test_cli_restore.py::test_cli_restore_passes_original_source_path`.
- `pytest tests/ -v` is green and `make lint` and `make test` exit zero. The four inverted
  clean-restore assertions named in the Architecture section (`tests/test_agent_protocol.py:198`,
  `:229`, `:258`, `:285`) still pass unchanged, and `git diff <pre-phase-sha> --
  tests/test_agent_protocol.py` reports no edit inside `:198-546`: they belong to the phase that
  moves `execute_restore`, and this phase must neither fix them nor rely on them.

## Phase 1 - config unification

Implements D5. One client, one config file, both modes on one machine. No serverless behaviour ships
here; this phase creates the file Phases 4-6 write into and repairs the enrol-to-service handoff that
D5 uses as its proof. Under D8 that handoff is a process boundary rather than an import boundary, and
this phase's work is more load-bearing for it, not less: a C# client must read a documented file
format, so `config.yaml` stops being an internal convenience and becomes a published contract
(Appendix B1).

**Status.** Largely on disk. `src/backer/core/config.py` holds the `BackerConfig` schema, loads and
saves `config.yaml`, rejects unknown top-level keys, and carries `migrate_legacy`;
`core/paths.py` resolves the one config directory (Phase 0's 10d/10e). Not verified here: that every
one of the four legacy surfaces in step 1 is covered by the migration, and the D5 heartbeat proof,
which needs a Windows host with a reachable server.

1. The four surfaces today. The migration in step 5 is complete only against this list.
   - `agent.yaml`, located by `get_config_dir()` (`client/agent.py:59-77`), written by
     `BackerAgent._save_credentials` (`:174-191`) and again by the wizard
     (`client/setup_wizard.py:116-133`). Exactly three keys: `server_url`, `client_id`,
     `client_secret` (`:180-184`). `chmod 0600` on POSIX only (`:190-191`).
   - `config.json`, a legacy artefact and no longer a live surface: the deleted Tk GUI's private
     file at `%APPDATA%\Backer\config.json`, JSON, in the same directory as `agent.yaml`. Four keys:
     `server_url`, `client_id`, `client_secret`, `hostname`. Nothing writes it now - the desktop
     client never writes config at all (D8) - but any machine upgraded from a pre-Avalonia build
     still has it, and it may hold the only enrolment credentials on that machine, so step 5 must
     read it. Migration-only, in both directions of the word: read it, never write it.
   - `backer.core.config`, a complete pydantic schema that nothing loads: `BackerConfig` with
     `mode`, `jobs` and per-job `ScheduleConfig`/`RetentionConfig` (`core/config.py:76-86`,
     `:41-53`), YAML `load`/`save` at `:88-99`. Its paths are POSIX-shaped with no platform branch
     and no env override: `~/.config/backer/config.yaml` (`:109-112`) and `~/.local/share/backer`
     (`:115-119`). Its only importers under `src/` are the re-export shim `core/__init__.py:3-6` and
     `core/job.py:12`.
   - Server state, which is not a file on the client at all. Jobs, repositories, schedules and run
     history live in the server's SQLite database (`Storage(data_dir / "backer.db")`,
     `server/app.py:3124`) and reach the agent only inside the heartbeat command payload
     (`client/agent.py:214-226`). Nothing of a server-managed job is therefore migratable: a machine
     that loses its server has nothing on disk to convert, which is why Phase 4 adopts from the
     repository sidecar instead.

2. Define `config.yaml`, YAML, one file per install. No new dependency: `pydantic`, `pyyaml` and
   `croniter` are already base dependencies (`pyproject.toml:31-34`). Format, an annotated example
   and the four install paths are the table in Appendix B1.

   - Resolution order for `get_config_dir()`: `BACKER_CONFIG_DIR`; then the per-user path; then the
     machine-scoped path only if it exists. The env override already exists at
     `client/agent.py:66-68`, so both shipped installers keep working untouched
     (`scripts/install-agent.sh:180` pins `/etc/backer`, `agent/service_entry.py:15-16` pins
     `%ProgramData%\Backer`). The last clause is the fix: Linux is hardcoded to `/etc/backer` with
     no XDG branch today (`client/agent.py:75-77`), so an unprivileged desktop user has nowhere to
     write, while a root install has no per-user file and must still be read.
   - Resolution lives in one new module, `src/backer/core/paths.py`, holding `get_config_dir` moved
     out of `client/agent.py:59-77`. Leave `backer.client.agent.get_config_dir` as a one-line
     re-export for one release so `cli.py:597`, `cli.py:826` and `client/setup_wizard.py:19` keep
     compiling; Phase 2 repoints them and adds the rest of the module.
   - Top-level keys, in this order and no others: `agent_id`, `server`, `repositories`, `jobs`.
     `agent_id` is `str(uuid4())[:8]`, generated on first write; on an enrolled machine it IS the
     server's `client_id`. `server:` is absent entirely on a pure serverless install.
     `repositories:` is keyed by `repo_id` and `jobs:` by job name, so neither can hold a duplicate
     and neither needs a linear scan.
   - There is no `mode:` key, at any level. A job naming a repository from `repositories:` is
     serverless; a server-managed job is dispatched inside the heartbeat payload and is never
     written to this file, because a second copy on disk could only ever be stale. That is the whole
     of the mode question and it is answered here and nowhere else.
   - Secrets are never inline. A repository carries `passphrase_ref` and `storage_password_ref`,
     whose values are the keystore keys `backer/repo/{repo_id}/passphrase` and
     `backer/repo/{repo_id}/storage`. The single exception is `server.client_secret`, which stays
     inline exactly as `agent.yaml` holds it now, because Phase 1 must not depend on Phase 4. The
     move is owned by Phase 4 step 1 and by nothing else: it writes the value to the keystore key
     `backer/server/{client_id}/secret` and replaces the field with `client_secret_ref`, and
     `BackerAgent.from_config` reads the ref when present and the inline value otherwise, so a
     `config.yaml` written by Phase 1 keeps working and the migration is one-way. Until that step
     lands `ClientConfig` carries `client_secret` verbatim, and Appendix B1's example shows it
     inline with that Phase 4 replacement named in the comment beside it.
   - `BackerConfig.save()` writes through the same temp-plus-`os.replace` helper as the sidecar and
     applies `chmod 0600` on POSIX, which `core/config.py:95-99` omits today. On Windows the
     machine-scoped copy inherits the `icacls` hardening at `client/windows_service.py:52-68`; the
     per-user copy is left to `%APPDATA%` ACLs.

3. One data directory. `get_data_dir()` gains a `BACKER_DATA_DIR` branch as its first check,
   matching `tools/manager.py:54`; `client/agent.py:80-91` ignores that variable today.
   - Everything written per run lives under `get_data_dir()`: `schedule.json`, `runs/`,
     `progress/{run_id}.json`, `run.lock`, `last_attempt/`. There is no `get_state_dir(config_path)`
     helper and none is added.
   - This is the fix for the SYSTEM-versus-user split brain. Under the SYSTEM boot task config
     resolves to `%ProgramData%\Backer` while `get_data_dir()` resolves to
     `C:\Windows\System32\config\systemprofile\AppData\Local\Backer`, so the scheduler and the
     interactive user would keep separate schedules, separate run locks and separate history: every
     scheduled job also runs interactively and neither identity sees the other's runs. Phase 4 sets
     `BACKER_DATA_DIR=%ProgramData%\Backer` on the SYSTEM task, which the `setdefault` at
     `agent/service_entry.py:13-14` already yields to.
   - The same branch makes `backer agent uninstall` stop reporting a data directory that does not
     exist (`cli.py:826`, `:838-839`).
   - DANGER, established by trying it in Phase 0 and reverting: adding the `BACKER_DATA_DIR` branch
     on its own is a data-loss bug, not an improvement. `install.sh` exports
     `BACKER_DATA_DIR=/var/lib/backer` in the systemd unit (`:390`) and in the
     `/usr/local/bin/backer` wrapper (`:407`), so on a host installed that way every `backer`
     invocation carries it - and `backer agent uninstall` does
     `shutil.rmtree(get_data_dir(), ignore_errors=True)` (`cli.py:963`). Making `get_data_dir()`
     honour the variable therefore points that `rmtree` at the server's own database on any host
     running both roles. This branch may only land together with a change to that uninstall path so
     it can never remove a directory the server owns. Land them in one commit, and give the pair its
     own acceptance bullet.

4. Make a bad config loud. `load_config` returns a default `BackerConfig()` whenever the path does
   not exist (`core/config.py:122-130`), which under one shared file means a typo in
   `BACKER_CONFIG_DIR`, a config the SYSTEM task cannot read, or a hand-edited file that fails
   validation all present as a client with zero jobs, and the scheduler then does nothing at all and
   says nothing about it.
   - Reaching this state without a legacy pair to migrate is a fresh install, and returning an empty
     `BackerConfig` is correct: `backer job list` prints the empty state, not an error.
   - A `config.yaml` that exists but does not parse or does not validate raises, naming the resolved
     path and the pydantic error. It is never replaced by defaults and never overwritten. Atomic
     writes mean a half-written file cannot occur, so a parse failure is a hand edit or a disk fault
     and the user must see it.
   - Unknown top-level keys are rejected rather than dropped, so a file written by a newer client is
     not silently degraded into one with no jobs by an older one.

5. Migrate on first load, in `backer.core.config.migrate_legacy()`, called from `load_config()` when
   no `config.yaml` resolves.
   - Search both `%APPDATA%\Backer` and `%ProgramData%\Backer` on Windows, and `get_config_dir()`
     plus `/etc/backer` on Linux. The old GUI wrote the first and the service reads the second;
     missing that pairing is the D5 defect itself, and a machine that hit it is exactly the machine
     with a stranded `config.json` to migrate.
   - Read `agent.yaml` (three keys) and `config.json` (four keys) and merge into `server:`,
     preferring `agent.yaml` on conflict because it is the file the agent itself wrote. Drop
     `hostname`; it is recomputed by `socket.gethostname()` at `client/agent.py:115`. Set `agent_id`
     to the migrated `client_id` when there is one, else generate it.
   - When both a per-user and a machine-scoped legacy pair exist and their `client_id`s differ, take
     the pair from the directory `get_config_dir()` resolves to for this process and log the one
     skipped. Never merge two different `client_id`s into one `server:` block; that would produce a
     config no server recognises.
   - Migration is idempotent and runs once: it is skipped entirely as soon as a `config.yaml`
     resolves, so a second `load_config()` neither rewrites the file nor re-reads the legacy pair.
   - Write `config.yaml` to `get_config_dir()`. Leave `agent.yaml` and `config.json` on disk
     untouched for one release so an already-installed frozen `backer-agent-service.exe` still
     boots, and note both for deletion in CHANGELOG.md. The window covers one more artefact than it
     used to: the frozen Tk `backer-agent.exe` a user may still have installed, which the rewritten
     installer replaces with `backer.exe` plus the desktop client (Phase 7 step 10). Do not shorten
     the window to one release on the strength of the new installer alone - a machine that never
     runs the new installer is the machine that needs the legacy files most.

6. Repoint the loader and every writer. Copying a file into place is not enough on its own:
   `agent/service_entry.py:15-16` pins `BACKER_CONFIG_DIR` and `:20` calls
   `BackerAgent.from_config().run()`, while `from_config` hardcodes `get_config_dir() /
   "agent.yaml"` (`client/agent.py:198-199`) and raises `FileNotFoundError` at `:201-202`. Without
   this step the service installs and then fails at every boot.
   - `BackerAgent.from_config`: call `backer.core.config.load_config()`, take `server_url`,
     `client_id` and `client_secret` from `server:`, and set `self.config_path` to the file actually
     loaded. Fall back to `<config_dir>/agent.yaml` with today's behaviour, and keep the
     `FileNotFoundError` when neither file exists.
   - `BackerAgent._save_credentials` (`:174-191`) read-modify-writes only the `server:` block and
     leaves `repositories:` and `jobs:` untouched. Its signature is whatever Phase 0 left it as when
     fixing the two-argument call at `cli.py:655` against the zero-argument definition; this step
     does not change it again.
   - `client/setup_wizard.py:37`, `:120` and `:225` are three copies of `get_config_dir() /
     "agent.yaml"`. All three go through the unified loader.
   - There is no GUI config wrapper to write. An earlier draft made the Tk GUI's
     `load_config`/`save_config` thin wrappers over the unified file; that code is deleted. Its
     replacement is a contract, not a function: `config.yaml` as B1 specifies it is what the desktop
     client reads, and the client performs no write of its own (D8), so the set of processes that
     may write this file is exactly `backer` and nothing else. State that in B1 and enforce it by
     having no other writer exist.
   - `_prepare_service_config` (`client/windows_service.py:40-76`) copies `config.yaml` when
     present, else `agent.yaml`, keeping the icacls hardening and its rollback-on-failure path
     (`:69-76`) unchanged. Update the three tests at `tests/test_windows_packaging.py:79-140`, which
     seed `agent.yaml` today.
   - `backer agent setup` calls `shutil.rmtree(config_dir)` at `cli.py:604`. Under one shared file
     that destroys serverless repositories and jobs; narrow it to clearing the `server:` block.
   - Two copies of `config.yaml` still exist on a Windows machine that runs the service, and that is
     deliberate: `_prepare_service_config` copies the per-user file to the machine-scoped path at
     install time, and the SYSTEM task reads only the machine-scoped one because
     `agent/service_entry.py:15-16` pins `BACKER_CONFIG_DIR` to it. They diverge if the user later
     edits the per-user file, so a serverless job the SYSTEM task must run belongs in the
     machine-scoped file; Phase 5's install path is what re-copies. State this in the install output
     rather than syncing the two behind the user's back.

7. Settle the two dead modules. Read `core/config.py` at HEAD before starting: commits `e8c2c2d`,
   `f3909c4` and `d843681` already stripped the engine fields this step used to have to delete, so
   the module is smaller than the plan's earlier description of it.
   - `core/config.py` becomes the unified schema, rewritten in place. `BackerConfig` and
     `load_config` keep their names. `ClientConfig` (`:67-73`) is kept verbatim and becomes the
     `server:` block, since it is already exactly the `agent.yaml` shape; the `BackerConfig.client`
     field (`:82`) is renamed `server`, a name freed by deleting `ServerConfig`. `SourceConfig`
     (`:10-15`), `ScheduleConfig` (`:34-38`) and `RetentionConfig` (`:24-31`) are unchanged. Add
     `RepositoryConfig`, and rework `JobConfig` (`:41-53`) to carry `repository: str` in place of
     `destination`. `jobs` and `repositories` become mappings, so `get_job` (`:101-106`) becomes a
     dict lookup. Delete `ServerConfig` (`:56-64`), `DestinationConfig` (`:18-21`), `mode`,
     `defaults`, `version`, `log_level`, `log_file` (`:79-86`), `get_default_config_path`
     (`:109-112`) and `get_state_dir` (`:115-119`); the first two are dead once `server:` is
     `ClientConfig` and a job names a repository, and the key list in step 2 is exhaustive. Path
     resolution lives in `backer.core.paths`, which `core/config.py` imports.
   - `RepositoryConfig` carries no `backend` field and `JobConfig` gains no `backend_options`, on
     disk or on the wire. Both are already gone from this module: `DestinationConfig` is the single
     field `path` at `:18-21`, and `JobConfig` (`:41-53`) has no options mapping.
     `tests/test_protocol_contract.py:56-113` rejects `backend`, `backend_type` and
     `backend_options` on the job and repository APIs — a `ValidationError` on `JobCreate` at `:63`
     and a 422 from the update and repository endpoints at `:85` and `:99`. The client schema must
     not reintroduce on disk a field the wire refuses; the wire name for
     per-repository settings is `repository_options` (`client/agent.py:890`, `:1239`;
     `server/app.py:594`) and that is the name `RepositoryConfig` uses for anything the backend
     needs beyond the passphrase ref.
   - `core/job.py` is not deleted. `BackupJob` (`:75-157`) goes, and with it the
     `JobConfig.schedule` and `JobConfig.retention` handling that never existed: nothing in
     `BackupJob.run` (`:86-140`) reads either field. Deleting it also removes a path already dead
     at HEAD: `run` (`:104`), `restore` (`:149`) and `list_snapshots` (`:155`) each call
     `get_backend("kopia", {})`
     with an empty config, so `_has_repository_password` is false and all three fail inside
     `_connect_repo` (`backends/kopia.py:181-182`) before kopia is ever spawned. `JobStatus`
     (`:15-22`) is kept and `JobRun` (`:25-72`) stays as the run-record type, both unchanged:
     `JobRun` annotates `status: JobStatus` at `:31`, serialises `self.status.value` at `:43` and
     parses `JobStatus(data["status"])` at `:65`, so deleting the enum would raise `NameError` on
     `import backer.core.job` and `tests/test_public_api_compat.py:5` would fail to collect.
     `_save_run_history` (`:159-166`) and `get_run_history` (`:168-184`) move to
     `backer/serverless/store.py` as module-level `append_run(data_dir, run)` and
     `read_runs(data_dir, job, limit)`, with `get_data_dir()` replacing the POSIX-only
     `get_state_dir()` they call today and that `core/job.py:12` imports.
   - `tests/test_core_job.py` is deleted along with `BackupJob`. Its one test,
     `test_backup_job_uses_kopia_without_removed_backend_options` (`:10-33`), imports both
     `BackupJob` (`:7`) and `DestinationConfig` (`:6`) and constructs
     `BackupJob(JobConfig(..., destination=DestinationConfig(path="/repo")))` at `:27-29`, so it
     cannot survive either deletion, and it asserts nothing the unified schema keeps. Record it in
     CHANGELOG.md beside the `backer.core.BackupJob` removal.
   - `core/__init__.py:4` becomes `from backer.core.job import JobStatus` and `:6` loses only
     `"BackupJob"`. `tests/test_public_api_compat.py` keeps `import backer.core.job` at `:5`,
     because the module still exists, and loses only the assert at `:13`; `:11-12` and `:14` stay
     green because `BackerConfig`, `load_config` and `JobStatus` all survive. Record the removal of
     `backer.core.BackupJob` as breaking in CHANGELOG.md.

Acceptance:

- `tests/test_config_unification.py::test_agent_yaml_and_legacy_json_merge_into_one_config` passes: a
  directory seeded with both legacy files yields a `config.yaml` whose `server:` block carries
  `server_url`, `client_id` and `client_secret`, whose `agent_id` equals that `client_id`, and both
  legacy files still exist afterwards.
- `tests/test_config_unification.py::test_migration_finds_legacy_config_in_program_data` passes: an
  `agent.yaml` present only under `%ProgramData%\Backer` still migrates.
- `tests/test_config_unification.py::test_unprivileged_linux_config_dir_is_xdg` passes: with
  `BACKER_CONFIG_DIR` unset and no `/etc/backer/config.yaml`, `get_config_dir()` returns
  `$XDG_CONFIG_HOME/backer`, never `/etc/backer`.
- `tests/test_config_unification.py::test_etc_backer_is_used_when_it_holds_the_only_config` passes:
  with `/etc/backer/config.yaml` present and no per-user file, `get_config_dir()` returns
  `/etc/backer`.
- `tests/test_config_unification.py::test_data_dir_honours_backer_data_dir` passes on both
  platforms, and `grep -rn --include='*.py' "get_state_dir" src/ tests/` returns nothing.
- `tests/test_config_unification.py::test_save_is_atomic_and_private` passes: after
  `BackerConfig.save()` the directory holds no `config.yaml.*.tmp`, and on POSIX the file mode is
  `0o600`.
- `tests/test_config_unification.py::test_repository_config_rejects_engine_fields` passes:
  `RepositoryConfig.model_validate` raises on each of `backend`, `backend_type` and
  `backend_options`, matching what `tests/test_protocol_contract.py:56-113` asserts of the wire.
- `tests/test_config_unification.py::test_client_agent_reexports_config_dir` passes:
  `backer.client.agent.get_config_dir is backer.core.paths.get_config_dir`, so `cli.py` and
  `client/setup_wizard.py` are unedited by this phase.
- `tests/test_config_unification.py::test_from_config_reads_unified_file` and
  `::test_from_config_falls_back_to_agent_yaml` pass, the second asserting the original
  `FileNotFoundError` when neither file exists.
- `tests/test_config_unification.py::test_agent_setup_preserves_serverless_config` passes: `backer
  agent setup` against a `config.yaml` holding two repositories and one job clears `server:` and
  leaves both mappings intact.
- `tests/test_config_unification.py::test_invalid_config_raises_instead_of_defaulting` passes: a
  `config.yaml` whose YAML is malformed raises with the resolved path in the message, and the file
  is byte-identical on disk afterwards.
- `tests/test_config_unification.py::test_server_managed_and_serverless_coexist` passes: a
  `config.yaml` carrying `server:`, one repository and one job loads, `BackerAgent.from_config()`
  returns an agent with the right `server_url`, and `save()` round-trips all three blocks unchanged.
- `tests/test_config_unification.py::test_migration_runs_once` passes: two consecutive
  `load_config()` calls leave the `config.yaml` mtime and bytes unchanged after the first.
- `tests/test_config_unification.py::test_conflicting_legacy_pairs_do_not_merge` passes: with
  different `client_id`s in `%APPDATA%` and `%ProgramData%`, the emitted `server:` block carries
  exactly one of them and the log names the other.
- `tests/test_windows_packaging.py::test_service_config_honors_explicit_config_directory`,
  `::test_service_config_restricts_copied_credentials_to_system_and_administrators` and
  `::test_service_config_fails_when_acl_hardening_fails` pass against `config.yaml`.
- `tests/test_public_api_compat.py::test_core_public_imports_are_canonical` passes with only the
  assert at `:13` deleted, `:14` still asserting `core.JobStatus is job.JobStatus`, and `python -c
  "import backer.core.job"` still exits 0.
- `uv run pytest tests/` passes with the only edited test files being
  `tests/test_windows_packaging.py` and `tests/test_public_api_compat.py`, and the only deleted one
  `tests/test_core_job.py`.
- D5 proof, on a Windows host with the server reachable. Enrol through the desktop client only, with
  no `agent.yaml` and no `config.yaml` anywhere on disk beforehand. **The desktop client's install
  action shells `backer agent install --mode server`, which produces a scheduled task whose run
  leads to a heartbeat within 90 seconds.** That command exits 0 where `_prepare_service_config`
  previously raised `FileNotFoundError` carrying `Agent config not found`
  (`_prepare_service_config`, `client/windows_service.py`), because there is now one config file and the installer copies
  it. Then `schtasks /run /tn BackerAgentService`, and within 90 seconds (a)
  `%ProgramData%\Backer\logs\backer-agent-<YYYY-MM-DD>.log` contains `Backer agent service
  starting`, which `agent/service_entry.py:17-19` writes to that directory explicitly, and (b) `GET
  /api/v1/clients/{client_id}` reports `status` of `online` with a `last_seen` newer than the
  `schtasks /run`. A task that is merely created does not satisfy this bullet. The proof stays
  falsifiable without the client: the same three steps run with `backer agent install --mode server`
  typed directly, which is the form a CI leg can drive.

## Phase 2 - extraction into backer.core

Pure refactor. No defect is re-fixed, no branch re-ordered, no error string reworded. The point is
that one extracted engine serves both the server-managed `BackerAgent` and the serverless client, so
the twelve clean-restore tests at `tests/test_agent_protocol.py:198-546` guard the move in both
modes at once.

**Status.** Done as a package: `src/backer/core/` holds `paths.py`, `config.py`, `mounts.py`,
`destination.py`, `runner.py`, `smb_browse.py`, `keystore.py`, `messages.py`, `job.py`,
`recovery.py` and `repo_metadata.py`. Whether every listed call site now routes through them, rather
than keeping a copy, is not verified here.

Read that claim narrowly, because it was written against `ffe31b6` and is weaker at HEAD. Those
twelve tests guard the refactor, not the behaviour: `:198`, `:258` and `:285` now assert that a
restore matching nothing deletes the staged original and reports success, which is the opposite of
what their names say and is the defect Phase 0 fixes. This phase inherits whatever Phase 0 leaves
and changes exactly one thing about them, the module the backend factory is patched on.

Precondition, not a task: `src/backer/backends/s3.py` already exists and `grep -rn --include='*.py'
"backer.server" src/backer/backends/` returns nothing at HEAD, so the backend layer is already
severed from the server package and a serverless client already imports with no FastAPI installed.
Commit `0e3df43` moved the module there; `parse_s3_config` survives at `backends/s3.py:32` and
`kopia_s3_config` (`:63`) replaced `restic_s3_config`. Nothing in this phase moves it again, and no
`core/s3.py` is created.

1. `src/backer/core/paths.py` already exists from Phase 1 and owns `get_config_dir`. This step only
   adds `get_data_dir`, moved from `client/agent.py:80-91` as Phase 1 left it, and
   `get_job_subfolder`, moved from `server/repository_paths.py:6-8`, which then holds nothing and is
   deleted.
   - Public surface: `get_config_dir() -> Path`, `get_data_dir() -> Path`,
     `get_job_subfolder(job_name: str) -> str`. No fourth directory helper; Phase 4 adds the
     run-lock and progress paths when it has a caller. `get_job_subfolder` names sidecar
     directories only, `.backer/jobs/{job_subfolder}/`; it never names a kopia repository, since a
     repository record addresses exactly one kopia repository at the repository root and every job
     on it is separated natively by its source, `user@host:path`.
   - Repoint `server/app.py:50`, `cli.py:597`, `cli.py:826`, `client/agent.py:114`,
     `client/agent.py:198` and `client/setup_wizard.py:19`. `server/app.py:50` imports
     `get_job_subfolder as _get_job_subfolder`; keep the alias, since the private name is what the
     rest of that module calls. Keep both directory helpers re-exported from `backer.client.agent`
     so the repoint is an ordinary commit rather than an atomic one.

2. `src/backer/core/smb_browse.py`: move `smb_auth_file` (`server/repositories.py:19-67`),
   `ShareInfo` (`:77-83`), `DirectoryEntry` (`:86-92`) and `SMBBrowser` (`:95-288`), and re-export
   all four from `server/repositories.py` for the server's own callers. Phases 3 and 5 import them
   from `backer.core.smb_browse`; Phase 6 reaches the same enumeration through `backer repo discover
   --json` rather than by import (D8). Without this move they would reintroduce into `backer.core`
   the `backer.server` dependency that the `backends/s3.py` move already removed from `backends/`.

3. `src/backer/core/mounts.py` from `client/agent.py:497-732` and `agent/service.py:97-394`. Drop
   `self`, keep every body identical.
   - `is_smb_path(path: str) -> bool`, `is_nfs_path`, `parse_smb_path(path: str) -> tuple[str, str,
     str]`, `parse_nfs_path`, `check_cifs_available() -> bool`, `check_nfs_available`. There is no
     obscured-password helper to move: `704bd8f` deleted the rclone backend and the
     `_rclone_obscure_password` function with it, so the reversible-obscure objection this phase
     used to carry is gone.
   - `smb_mount_context(server, share, username=None, password=None, domain=None, *, cifs_check:
     Callable[[], bool] = check_cifs_available) -> Generator[Path, None, None]` and
     `nfs_mount_context(server, export_path, *, nfs_check=check_nfs_available)`, both still
     `@contextmanager`.
   - The two `*_check` parameters are the only signature change in this phase and they exist for one
     reason: `tests/test_agent_protocol.py:177` patches `_check_cifs_available` on the agent
     instance, and with no injection point that patch goes dead and the test passes vacuously
     against a mocked `subprocess.run`. `BackerAgent._smb_mount_context` passes
     `cifs_check=self._check_cifs_available`.
   - Move `SMBConnectionManager` (`agent/service.py:97-394`) whole, including
     `_connect_with_explicit_credentials` (`:277-327`), the machine-scoped path D6 depends on, and
     move `get_subprocess_flags` (`:40`) with it because every one of its subprocess calls uses it.
     Re-export both from `agent/service.py`: `:443` constructs the manager and
     `tests/test_agent_protocol.py:161` monkeypatches the name on that module. `601778f` already
     deleted the second execution engine that used to sit below it, so nothing else in that file is
     in scope and `agent/service.py` is 999 lines, not 2154.

4. `src/backer/core/destination.py` from `client/agent.py:733-862` and `:1077-1212`. These bodies
   are already driven entirely by the job dict.
   - `prepare_destination(job: dict[str, Any], backend_name: str) -> tuple[str, Any]` (from
     `_prepare_destination_for_backend`, `:733`) and `prepare_source(job, backend_name)` (from
     `:1077`). The second element stays the raw context manager the caller must exit; do not convert
     it here. The module-private helpers at `:804`, `:838`, `:1160` and `:1193` keep their bodies,
     including the narrowed `if backend_name == "kopia":` predicates at `:818`, `:847` and `:1174`
     that `0629044` left behind. `backend_name` still arrives as `"kopia"` or `"proxy"` because the
     runner derives it at `:901` and `:1250`; it is never the job's own vestigial `"backend"` key.
   - `prepare_windows_smb(path: str, job: dict[str, Any]) -> None` (from `:1147-1158`) loses
     `self._smb_manager` and holds the manager in a module-level lazy singleton. Keep the deferred
     `from backer.agent.service import SMBConnectionManager` at `:1153` exactly where it is; that
     import is the patch point `tests/test_agent_protocol.py:161` relies on. The singleton is
     process-wide, which is closer to the error-1219 pooling intent than the per-instance cache,
     since `agent/service.py:783` builds a fresh `BackerAgent` per command; note in the module
     docstring that a test needing a clean manager resets `backer.core.destination._smb_manager`.
   - That deferred import is the one back-edge from `backer.core` into `backer.agent`, and it is
     deliberate. It is function-local, so it creates no import cycle at module load and no `fastapi`
     dependency; do not tidy it to module scope, because that is what makes the test patch dead.

5. `src/backer/core/runner.py` from `client/agent.py:864-1075` (`execute_backup`) and `:1214-1493`
   (`execute_restore`), taking `_backend_for_location` (`:50-56`) with them because it is their only
   backend construction site and the one function that calls `get_backend`. Their only server
   coupling is `_report_progress` (`:472-495`) and the four `client.post("/api/v1/results", ...)`
   calls at `:1012`, `:1063`, `:1433` and `:1479`.

   ```python
   def run_backup(
       job: dict[str, Any],
       *,
       dry_run: bool = False,
       on_progress: ProgressCallback | None = None,
       on_result: ResultCallback | None = None,
       agent_credentials: tuple[str, str] | None = None,
   ) -> dict[str, Any]: ...

   def run_restore(...) -> dict[str, Any]: ...   # same keywords
   ```

   - `ProgressCallback` is `Callable[..., None]`, called only by keyword, with exactly the parameter
     set of `_report_progress`: `run_id: str`, `status`, `progress_percent`, `current_file`,
     `bytes_processed`, `files_processed`, `message`, all optional. It matches the bound method so
     `monkeypatch.setattr(agent, "_report_progress", ...)` keeps working in all twelve clean-restore
     tests.
   - `ResultCallback` is `Callable[[dict[str, Any]], None]`, called as `on_result(report)` with the
     same dict the function returns. `None` for either callback is a no-op, which is why serverless
     mode needs no HTTP client at all.
   - Keep the four `try`/`except` wrappers around `on_result` with today's exact handling; the
     stdout differs per site and is not interchangeable: `Failed to report result: {e}`
     (`:1013-1014`), `[BACKUP] Failed to report error to server: {report_err}` (`:1064-1065`),
     `Failed to report restore result: {e}` (`:1434-1435`), and a bare `except Exception: pass` on
     the restore failure path (`:1480-1481`).
   - This is the one place where the extraction is not merely "move the body", because the body now
     wraps a stateful connection. Kopia names no repository on any command line: every operation is
     `repository connect` → do → `repository disconnect`, bracketed by the `finally` at
     `backends/kopia.py:383-384` for backup and `:581-582` for restore, and the connection identity
     lives in a config file that the backend instance's environment points at. Three rules follow,
     and all three are shape rather than fix; Phase 0 owns the defects themselves.
   - Build exactly one `KopiaBackend` per `run_backup`/`run_restore` call, inside the function, via
     `_backend_for_location`, and never cache it across calls or share it between threads. Its
     `_env` carries `KOPIA_PASSWORD` (`backends/kopia.py:43-48`), the per-repository
     `KOPIA_CONFIG_PATH` and `KOPIA_CACHE_DIRECTORY` that Phase 0 adds, and the AWS credentials that
     `_get_repo_type` (`:98-104`) injects into that same env as a side effect. Two runs sharing one
     instance share one connection and one `finally: _disconnect_repo()`.
   - Nothing may reach the repository before the connect. Those S3 credentials exist only because
     `_get_repo_type` ran inside `init_repo` or `_connect_repo`, so any fast path that skips the
     connect loses them silently. Keep `backend.check_available()` where it is (`:904`, `:1253`): it
     is deliberately the one kopia invocation that runs with no `env=`
     (`backends/kopia.py:76-82`), so it is not a substitute for a connection probe.
   - The prepared destination must outlive the backend call. `prepare_destination` returns a live
     mount that kopia's connection is bound to; unwinding it before the disconnect leaves a
     connection open against a path that no longer resolves. Today the `finally` at `:1069-1075`
     exits the context after the backend returns, and `:1485` does the same for restore. Preserve
     that order exactly; do not tidy either into a `with` block that closes earlier.
   - `agent_credentials` must not collapse to an id. `client/agent.py:895-896` and `:1244-1245` set
     BOTH `client_id` and `client_secret` into `repository_options`, and the proxy backend, the only
     backend a server `local` repository ever uses, needs both to authenticate; carrying only the id
     silently breaks every server `local` repository. Unpack it as `client_id, client_secret =
     agent_credentials` inside the existing proxy-prefix branch — `if job.get("destination_path",
     "").lower().startswith(("proxy://", "proxys://")):` at `:894`, and its `source_path` twin at
     `:1243` — and leave the two assignments otherwise untouched. Note that this branch tests the
     path scheme, not `backend_name`, which is not assigned until `:901` and `:1250`.
   - `agent_credentials[0]` is also the `"client_id"` field of all four report dicts, at `:999` and
     `:1051` (backup success and failure) and `:1421` and `:1467` (restore success and failure).
     With `agent_credentials=None` that field is `None`. This is the single place agent identity
     enters the engine.
   - `get_backend` is imported at module level in the new module, and `_backend_for_location` moves
     with it, which together make `backer.core.runner.get_backend` the patch path in step 8; the
     twelve test sites change because the object they patch has moved, not because the call did.
   - `run_id` selection moves unchanged: `client/agent.py:871` already prefers a server-supplied
     `run_id` and falls back locally. Phase 4 replaces only the fallback.
   - Move `_redact_repository_options` (`:31-44`) and `_log_repository_options` (`:46-47`) into the
     module and re-export both from `backer.client.agent`; `tests/test_client_redaction.py:21` calls
     `_log_repository_options` directly and must keep passing untouched.
   - Move whatever Phase 0 left at `:1016-1017` unchanged. Phase 0 removed the `if result.success:`
     guard there, so the call to `_write_repo_metadata` stays unconditional and the failed-run
     record test Phase 0 added with it stays green across the move.

6. Move the sidecar writers with the engine, because the runner calls them: `_write_repo_metadata`
   (`:1495-1551`), `_write_metadata_to_path` (`:1553-1616`) and `_write_restore_metadata`
   (`:1618-1665`). Each takes `agent_id: str | None` where it read `self.client_id` (`:1575`,
   `:1588`, `:1600`, `:1657`), and calls `backer.core.mounts` where it called
   `self._smb_mount_context`/`self._is_smb_path`/`self._parse_smb_path` (`:1518-1525`). Their
   `RepositoryMetadata` payloads are unchanged here; Phase 4 fattens them.

7. Reduce `BackerAgent` to an adapter. Every extracted method keeps its name, signature and access
   level, so no caller outside the class changes. `execute_backup(job, dry_run=False)` becomes
   `return run_backup(job, dry_run=dry_run, on_progress=self._report_progress,
   on_result=self._post_result, agent_credentials=(self.client_id, self.client_secret))`, where
   `_post_result` is the extracted body of `:1011-1012` and calls `self._get_client()` so
   `monkeypatch.setattr(agent, "_get_client", ...)` still intercepts it; `execute_restore` mirrors
   it. `agent/service.py:779-787` (`_execute_with_shared_agent`) and its callers `_execute_backup`
   (`:789`) and `_execute_restore` (`:793`) are not touched. Delete `self._smb_manager`
   (`client/agent.py:125`) once `prepare_windows_smb` owns the singleton; `:1152-1154` is its only
   reader. What is left in `client/agent.py` afterwards is the HTTP client, registration, the
   heartbeat loop, command dispatch, browse-filesystem, progress and result posting, and the
   delegators, which is the whole of the server-managed adapter and nothing else.

8. Preserve the observable surface. Error strings, print prefixes, branch order and the shape of the
   returned `report` dict are asserted on and are the only thing distinguishing a correct move from
   a plausible one: `NFS destinations are not supported` and `NFS restores are not supported`
   (`tests/test_agent_protocol.py:139`, `:144`), and `rollback failed` in `report["errors"][-1]`
   (`:489`).
   - Exactly two symbol paths move in the test suite: `client_agent.get_backend` to
     `backer.core.runner.get_backend` (twelve sites, one sed) and `backer.server.repository_paths`
     to `backer.core.paths` (`tests/test_workflow_sanity.py:87`). Thirteen lines; no other test file
     is edited. `tests/test_s3.py` is untouched, because it already imports
     `backer.backends.s3` (`:18`).
   - Three patch points are deliberately NOT repointed, because they patch shared module objects
     rather than a `backer` module: `client_agent.sys` (`tests/test_agent_protocol.py:137`),
     `client_agent.subprocess` (`:189`) and `client_agent.shutil` (`:478`).
   - The existing tests that guard the move, all of which must stay green with no edit:
     `tests/test_agent_protocol.py:135` and `:150` (Windows NFS rejection, Windows SMB session
     setup) for `core/destination.py`; `:170` (Linux CIFS credentials file) for `core/mounts.py`;
     the twelve clean-restore tests at `:198-546` for `core/runner.py`; `:77`
     (`test_agent_metadata_does_not_write_backend`, which calls `_write_metadata_to_path` directly
     and asserts no `backend` key reaches the job document or the snapshot document) for step 6; and
     `tests/test_client_redaction.py`, `tests/test_service_dispatch.py`,
     `tests/test_agent_service.py`, `tests/test_backends.py` and `tests/test_repo_metadata.py` for
     everything the move touches indirectly. `tests/test_restic_restore.py` and
     `tests/test_restic_partial_restore.py` are not on this list: `704bd8f` deleted both.

9. Add `tests/test_core_runner.py`, the one thing the extraction is not otherwise covered by. None
   of the 21 tests in `tests/test_agent_protocol.py` asserts that the proxy branch populates
   `client_secret`: eleven of the twelve clean-restore tests carry `"backend": "kopia"`, the twelfth
   carries `"backend": "proxy"` (`:361`) and only asserts that clean restore is refused, and that
   key is vestigial anyway since the runner derives `backend_name` from the path scheme at `:901`
   and `:1250`. Patch `backer.core.runner.get_backend` with a recorder; call `run_backup` and
   `run_restore` with a `proxy://` `destination_path`/`source_path` and
   `agent_credentials=("agent", "secret")`; assert the captured `repository_options` carry
   `client_id == "agent"` and `client_secret == "secret"` alongside `location`; and assert
   `BackerAgent.execute_backup` forwards `(self.client_id, self.client_secret)`.

Acceptance:

- `pytest tests/ -v` collects the same number of tests as the commit before this phase plus the
  three in `tests/test_core_runner.py`, and `git diff --numstat <pre-phase-sha> -- tests/
  ':(exclude)tests/test_core_runner.py'` reports 13 added and 13 deleted lines across two files,
  being the thirteen symbol-path lines named in step 8. The exclusion is required because this phase
  adds `tests/test_core_runner.py`, whose whole body numstat counts as additions.
- `grep -rn --include='*.py' "from backer.server" src/backer/backends/` returns nothing, and so does
  `grep -rn --include='*.py' "backer.server" src/backer/core/ src/backer/client/ src/backer/agent/`.
  The first was already true before this phase and is asserted only to prove the move did not
  reintroduce it.
- `python -c "from backer.server.repositories import SMBBrowser, ShareInfo, DirectoryEntry; from
  backer.agent.service import SMBConnectionManager, get_subprocess_flags"` resolves, proving every
  re-export survived, and
  `grep -rn --include='*.py' "^class SMBBrowser\|^class SMBConnectionManager" src/` returns exactly
  two paths, both under `src/backer/core/`.
- `git ls-files src/backer/server/repository_paths.py` returns nothing, and `grep -rn
  --include='*.py' "server\.repository_paths" src/ tests/` returns nothing.
- `tests/test_core_runner.py::test_proxy_backup_passes_both_agent_credentials` and
  `::test_proxy_restore_passes_both_agent_credentials` fail if either `client_id` or `client_secret`
  is dropped from the proxy `repository_options`.
- `tests/test_core_runner.py::test_one_backend_instance_per_run` passes: the recorder patched over
  `backer.core.runner.get_backend` is called exactly once per `run_backup`, and two sequential calls
  receive two distinct instances, so no kopia connection is shared across runs.
- `tests/test_agent_protocol.py::test_linux_smb_password_uses_private_credentials_file` still fails
  when `smb_mount_context` is edited to put the password on argv, proving the `cifs_check` injection
  kept it non-vacuous.
- `tests/test_workflow_sanity.py:89` passes unchanged against `backer.core.paths`:
  `get_job_subfolder('Daily:VM/Backup?*') == "Daily_VM_Backup__"`, proving the sidecar naming stayed
  byte-identical across the move.
- `python -c "import backer.core.runner, backer.core.destination, backer.core.mounts,
  backer.core.smb_browse"` succeeds in an environment with no `fastapi` installed, and `grep -rn
  --include='*.py' "backer\.agent\|backer\.client" src/backer/core/` returns exactly one line, the
  deferred `SMBConnectionManager` import in `core/destination.py`.
- `ruff check src/ tests/` is clean, and `git diff --stat` shows `src/backer/client/agent.py`
  shrinking by at least 800 lines from its 1714 at HEAD.

## Phase 3 - SMB transport and discovery spike

D3 settles v1 without this spike: mount and UNC ship, because they work today - Linux mounts CIFS
and hands Kopia the mount path (`client/agent.py:818-831`), Windows opens the session and hands
Kopia the UNC path (`_prepare_windows_smb`, `client/agent.py:1147-1158`) - and neither adds a
binary. The spike therefore gates v2, not v1, and asks two questions that must not be conflated.

**Status.** Not run, by design - it gates v2. What is on disk from this phase's v1 half is
`core/smb_browse.py` and the `backer repo discover` command (share enumeration on one named host,
`--json`), plus the `serverless-smb-linux` and `serverless-smb-windows` CI jobs. Arm D stays dropped
for v2, as recorded below.

The first is new and exists only because the product is Kopia. Kopia has a native rclone provider,
`kopia repository create rclone --remote-path=REMOTE-PATH`, verified present in the pinned 0.23.1
binary (`tools/manager.py:20-38`), so an on-the-fly `:smb,host=...:` remote can carry backup DATA.
Restic's `--repo <path>` model made that impossible, and that impossibility is what the old D3
reasoned from; the reasoning is dead, the v1 conclusion is not. Note before starting that 0.23.1's
help text for that command reads `Create repository in a rclone-based provider [Not maintained]`.

The second question is unchanged: enumerating shares and directories on a host the user typed. No
arm sweeps a subnet, and no arm finds the host.

1. Commit `scripts/spike_smb_discovery.py`, a click command that stays in the tree so a new NAS
   can be retested. Flags `--arm {a,b,d} --server --share --device-label --dialect --depth`;
   credentials come only from `SPIKE_SMB_USER` and `SPIKE_SMB_PASS`.
   - Append one JSON record per attempt to
     `spike-results/{arm}-{device_label}-{dialect}.jsonl`: arm, platform, dialect, device label,
     share and directory counts or repository size, elapsed milliseconds per operation, the
     verbatim error, and `argv_leak`.
   - Before every `subprocess` call, assert that `SPIKE_SMB_PASS` appears in no element of the
     joined argument list, in plaintext and in `rclone obscure` form; a match sets `argv_leak` and
     exits non-zero. Checking both forms is what makes the arm-D gate reachable, because an
     obscured password is reversible with `rclone reveal` and protocolfixes.md Phase 0 item 3
     forbids a recoverable secret on a command line.

2. Arm D - Kopia over rclone as the DATA path. This is the only arm that can change a shipping
   decision, and it is the only new one.
   - Create and connect: `kopia repository create rclone --remote-path=:smb:<share>/<subpath>`
     with `RCLONE_SMB_HOST`, `RCLONE_SMB_USER` and `RCLONE_SMB_PASS` set in the child
     environment, which is the same route `KopiaBackend` already uses for `KOPIA_PASSWORD`
     (`backends/kopia.py:43-47`) and for the AWS keys (`backends/s3.py:78-81`). Record whether
     rclone reads them when spawned by kopia rather than by a shell. If it does not, the two
     documented alternatives both leak: `--rclone-env KEY=VALUE` puts the pair on kopia's argv,
     and the inline `:smb,host=..,pass=<obscured>:` form puts the obscured password there.
   - `--embed-rclone-config=PATH` writes the provider's rclone config into the kopia repository
     config, which after the Phase 0 item 5 fix is the per-repository `KOPIA_CONFIG_PATH` file.
     Record whether an embedded SMB remote carries the obscured password into that file. If it
     does, the arm fails on that alone: the storage credential would leave the keystore and land
     in a file on disk, which C3 and D4 do not permit.
   - Then run a real workload, not a probe: `snapshot create` of a 5 GB tree with 50k files,
     `snapshot list`, `snapshot restore` of one directory, and `snapshot verify
     --verify-files-percent=5`, each timed against the same tree written through the mount/UNC
     path on the same device so the number is a ratio and not an absolute. Record the failure
     modes an unattended agent meets: rclone absent, rclone exceeding `--rclone-startup-timeout`
     (default 15s), and the NAS dropping the connection mid-snapshot. A second process in the data
     path is a second thing that can hang at 02:00 with nobody watching.

3. Arm A - Windows discovery, native. `net view \\<server>` for shares, then the session through
   `SMBConnectionManager.connect` (`agent/service.py:108-210`) and stdlib `Path(unc).iterdir()`
   for directories. `net view` appears nowhere in the tree today - `grep -rn "net.*view"
   --include='*.py' src/` returns nothing - so parse its fixed-width output and record what it
   does with share names containing spaces. Both shipped credential paths put the password on
   argv, `cmdkey /add ... /pass` (`agent/service.py:265`) and the bare positional password
   (`:307`), so also measure `net use <unc> /user:<user> *` with the password on the child's
   stdin and no interactive console attached.

4. Arm B - Linux discovery. `SMBBrowser.list_shares` (`server/repositories.py:99`, `smbclient -L
   -g -t 5` at `:112`) and `list_directory` (`:164`, 5-second connect at `:196`), reached through
   `discover_shares` (`:545`) and `browse_directory` (`:582`), with credentials in an auth file
   rather than on argv - `smb_auth_file` (`:19`) documents that reason at `:24-27`. The arm
   starts argv-clean, so measure parsing instead: the directory-classifying regex at `:233` per
   device, and the `FileNotFoundError` path (`:159`, `:258`) with no `samba-client` installed.

5. Matrix. Three devices minimum: a `New-SmbShare` on the Windows runner, Samba in the
   `dperson/samba` container, and one physical consumer NAS named by model and firmware. Dialects
   2.0.2, 2.1, 3.0 and 3.1.1 pinned server-side, 3.x once signed and once encrypted. On every
   device also run a share name with a space, a directory with a space, a non-ASCII directory
   name and one guest share; the `net view` parse and the regex at `server/repositories.py:233`
   are the likely casualties, and `smbclient` takes `-N` (`:117`) where `net use` has no
   argv-free equivalent.

6. Pass bar, different per question. Discovery: shares under 5 seconds and a depth-1 directory
   listing under 2 seconds, matching the connection timeouts at `server/repositories.py:112` and
   `:196`; anything that completes only inside the 30-second outer timeout at `:124` is a failure
   for wizard purposes. Arm D: no `argv_leak`, no credential in the repository config, every
   operation succeeding on all three devices at every dialect, and a snapshot wall time within
   1.25x of the same device over mount/UNC. Arm D fails the whole gate if any single cell fails,
   because the fallback costs nothing and adopting rclone costs a binary.

7. Price the dependency before reading the results, and record it as a line in the results table.
   `TOOL_INFO` holds kopia alone (`tools/manager.py:20-38`), pinned to 0.23.1 and verified against
   the publisher's checksum manifest (`:24`). Adding rclone is a second pinned version, a second
   checksum source, a second binary in the installer and a second process in the data path, on a
   kopia provider its own help text marks unmaintained.

8. Default if the spike is inconclusive - fewer than two devices reached per arm, or arms that
   tie: ship arm A on Windows and arm B on Linux for discovery, and keep mount and UNC as the data
   path. That is the v1 default already, so an inconclusive spike changes nothing and delays
   nothing. rclone is not added to break a tie.

9. Write the outcome into this section under a `### Spike results (YYYY-MM-DD)` heading, one row
   per attempt:

   | Arm | Platform | Device / firmware | Dialect | Op | Count | ms | Credential on argv | Notes |

   Follow it with two decision lines. The first names the discovery implementation chosen per
   platform and the `backer.core.smb_browse` entry points they become, `list_shares(server,
   credentials)` and `list_directories(server, share, path)`. The second states whether arm D is
   adopted for v2 or dropped, in one sentence, with the cell that decided it. Fold the discovery
   winner in and delete the losing arm; an unexercised fallback is a second untested code path.

Acceptance:

- `python scripts/spike_smb_discovery.py --arm b --server <host> --share <share>` exits 0 and
  appends at least one JSON record to `spike-results/`.
- A deliberate arm-D run using the inline `:smb,host=..,pass=..:` remote form exits non-zero with
  `argv_leak` set, because the check tests the `rclone obscure` form as well as the plaintext.
- `tests/test_smb_browse.py::test_no_password_on_argv` builds every command
  `backer.core.smb_browse` emits with a sentinel password and asserts the sentinel appears in no
  argv element.
- This section carries a `### Spike results (YYYY-MM-DD)` heading with one row per attempt, every
  unreached cell marked `unreachable: <reason>` rather than blank, and the two decision lines
  from item 9; when fewer than two devices were reached per arm it records the item 8 default
  explicitly.
- `grep -n "rclone" pyproject.toml src/backer/tools/manager.py` returns nothing after this phase,
  in every outcome except an explicit v2 adoption decision recorded under the results heading.

### Spike results (2026-09-01)

| Arm | Platform | Device / firmware | Dialect | Op | Count | ms | Credential on argv | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | Windows 10.0.26200 | local Windows share `Script` | local | `net view`, depth-1 listing | 2 shares, 6 entries | 55, 1 | no | Native discovery completed locally; this is not a consumer NAS result. |
| B | Windows 10.0.26200 | local Windows share `Script` | local | `smbclient` discovery | unreachable | 6 | no | unreachable: `smbclient` is not installed. |
| D | Windows 10.0.26200 | local Windows share `Script` | local | rclone provider preflight | unreachable | n/a | no | unreachable: rclone is not installed. |
| D | Windows 10.0.26200 | argv guard fixture | local | inline remote secret | n/a | n/a | rejected | `argv_leak` correctly exited non-zero; the recorded remote redacted the secret. |
| A/B/D | Windows/Linux | Samba (`dperson/samba`) | 2.0.2, 2.1, 3.0, 3.1.1 signed/encrypted | matrix | unreachable | n/a | n/a | unreachable: no pre-existing Samba container/image; pulling one would persist a machine change. |
| A/B/D | Windows/Linux | consumer NAS / firmware unavailable | 2.0.2, 2.1, 3.0, 3.1.1 signed/encrypted | matrix | unreachable | n/a | n/a | unreachable: no physical consumer NAS credentials or device were available. |
| D | Windows/Linux | dependency price | n/a | packaging assessment | n/a | n/a | n/a | rclone would add a pinned binary, checksum source, installer payload, and subprocess; Kopia marks its rclone provider unmaintained. |

Discovery decision: use native Windows `net view` plus `Path(unc).iterdir()` for `backer.core.smb_browse.list_shares(server, credentials)` and `list_directories(server, share, path)`; use the existing Linux `SMBBrowser`/`smbclient` auth-file path for those entry points. With fewer than two devices reached per arm, item 8 applies explicitly: ship arm A on Windows and arm B on Linux, and keep mount and UNC as the data path.

Arm D is dropped for v2 until a future complete matrix rerun; the deciding cell was the local Windows preflight, which was unreachable because rclone is absent, while fewer than two devices reached also requires the item-8 default.

## Phase 4 - the serverless engine

Every step consumes `core/runner.py` from Phase 2; no backup or restore logic is rewritten. New
code is `src/backer/core/keystore.py` plus a thin `src/backer/serverless/` package composing
core: `schedule.py` (due calculation), `retention.py` (policy and expiry plumbing), beside the
`store.py` Phase 1 already moved. The `backer repo` group and the `job run`/`job history`
subcommands this phase's Acceptance drives are written here, spelled exactly as the Phase 5 tree
declares them; Phase 5 adds the wizard, the progress rendering, the no-TTY contract and the rest
of the surface on top of them.

**Status.** On disk and wider than this paragraph describes: `src/backer/core/keystore.py` exists,
and `src/backer/serverless/` is the full module list given above - `cells.py`, `modes.py`,
`repositories.py`, `runs.py`, `history.py`, `sidecar.py`, `s3_sidecar.py` and `scheduled_test.py`
beside the `schedule.py`, `retention.py` and `store.py` this paragraph names. `keystore status
--json` is a shipped command. Not verified here: the D6 unattended proof, which needs a Windows host.

v1 repository types are `local`, `smb` and `s3` (D2). S3 is the cheapest of the three: nothing to
mount, nothing to enumerate, no privilege story, and a validated credential boundary that already
ships at `backends/s3.py`.

1. Build `src/backer/core/keystore.py` (D4), stdlib only.
   - `put(key, value, *, machine_scope=False) -> str` returns the backend used (`"dpapi" |
     "secret-tool" | "file"`); `get` and `delete` take the same keyword. Callers print the backend
     name, never the value.
   - Windows is `ctypes` against `crypt32.CryptProtectData` / `CryptUnprotectData`, where
     `machine_scope=True` sets `CRYPTPROTECT_LOCAL_MACHINE` and is what makes a secret readable by
     the SYSTEM task in step 8; blobs are files named `sha256(key)` under
     `%APPDATA%\Backer\secrets\` or `%ProgramData%\Backer\secrets\`, ACL'd with the `icacls` shape
     at `client/windows_service.py:52-68` plus `/inheritance:r`. Linux is `secret-tool store` /
     `lookup` under `service backer key <key>` when `shutil.which("secret-tool")` and
     `DBUS_SESSION_BUS_ADDRESS` are both present.
   - Headless fallback: a `0600` file in a `0700` directory under the data dir, used only when
     Secret Service is absent. `put` returns `"file"`, the caller says it downgraded, and
     `--headless` makes the choice explicit. Do not encrypt that file with a key stored beside it;
     `server/secrets.py:1-10` documents why that is not protection. Threat model, in the docs
     verbatim: machine-scoped DPAPI is decryptable by any process on that machine, so the boundary
     is the ACL and not the cryptography; the file fallback is protected by mode alone; neither
     survives an attacker with administrative access.
   - Keys, and there are only three shapes: `backer/repo/{repo_id}/passphrase` holds the Kopia
     repository encryption passphrase; `backer/repo/{repo_id}/storage` holds whatever credential
     reaches the storage, an SMB password as a string or the S3 `access_key_id` and
     `secret_access_key` as one JSON object; `backer/server/{client_id}/secret` holds the server
     enrolment secret. Names derive from the record id, never a host or a username, so two
     repositories on one NAS cannot collide.
   - Move `server.client_secret` into `backer/server/{client_id}/secret` and replace the key with
     `client_secret_ref`. `BackerAgent.from_config` reads the ref when present and the inline
     value otherwise, so a config written by Phase 1 keeps working and the migration is one-way.
     This is what puts the third key shape above to use; no other step performs the move.

2. Own Kopia's connection lifecycle explicitly. Kopia is connection-oriented where restic was
   stateless: no command names a repository, so every operation is connect - do - disconnect
   (`backends/kopia.py:253`, `:487`, `:590`, `:651`, `:757`, `:809`, each with a matching
   `finally`). Phase 0 item 5 lands the isolation - `KOPIA_CONFIG_PATH` and
   `KOPIA_CACHE_DIRECTORY` per repository plus the `_serialized_kopia_operation` + `file_lock`
   mutex ported from `server/app.py:78-86`. This phase consumes that fix and does not restate it.
   What it owns is the three things a mutex does not answer.
   - A crash leaves the connection open. `_disconnect_repo` (`backends/kopia.py:205-218`) runs
     only from a `finally` and swallows every exception (`:216-218`), so a killed agent or a
     SYSTEM task terminated at shutdown leaves the per-repository config recording a live
     connection. The runner therefore treats that config as disposable: before every operation it
     runs `repository disconnect` against the repository's own `KOPIA_CONFIG_PATH`, ignores the
     result, and connects fresh. Idempotent, one extra process spawn, and it removes "recover a
     stale connection" as a state anyone has to reason about.
   - `--persist-credentials` is ON by default. Kopia 0.23.1 accepts it on every command
     (`$KOPIA_PERSIST_CREDENTIALS_ON_CONNECT`) and no code path passes the negation, so connecting
     writes the passphrase into the machine's credential store. Verified against the pinned
     binary: after `repository create`, `kopia repository status` succeeds with `KOPIA_PASSWORD`
     unset; `repository disconnect` reports `deleted repository password for <config path>` and a
     later connect prompts again. So the exposure is bounded by the disconnect, which is exactly
     what a crash skips. Decision: pass `--no-persist-credentials` on every serverless `repository
     create` and `repository connect`, and never pass `--use-credential-manager`. Verified this
     holds - with the flag, `repository status` and `snapshot create` both succeed while
     `KOPIA_PASSWORD` is in the environment and fail closed without it. The cost is one keystore
     read per operation; the gain is that the passphrase lives in exactly one place a user can
     point at, which is the premise of D4.
   - The S3 credential enters the environment as a side effect of `_get_repo_type`
     (`backends/kopia.py:98-104`), which only `init_repo` and `_connect_repo` call, so any fast
     path that skips the connect silently loses it. The invariant, stated once: no kopia
     subcommand runs except inside a connect-do-disconnect block owned by the runner.

3. Add the repository record and split `repo add` into create-vs-attach. One record addresses
   exactly one Kopia repository, at the repository root as this machine reaches it. Every job on
   the record shares it and is separated by source, per step 9.
   - `RepositoryConfig` sits beside `JobConfig` in `core/config.py`: `id` (the key, and the
     keystore namespace), `name` (display text), `type` (`local` | `smb` | `s3`), `path` (the
     repository root as a local path, or as a subpath inside the share), `server`, `share`,
     `username`, `domain`, `scope`, `unique_id`, `added_at`, plus mutable state
     `last_check_status`, `last_check_at`, `use_existing_session`. There is no `backend` field:
     `DestinationConfig.backend` and `JobConfig.backend_options` no longer exist, the wire format
     is `repository_options` (`client/agent.py:895-896`, `server/app.py:594`), and
     `tests/test_protocol_contract.py:56-113` rejects `backend`, `backend_type` and
     `backend_options` outright. No secret values, only the key names from step 1.
   - An `s3` record additionally carries `bucket`, `prefix`, `endpoint`, `region` and
     `path_style`, validated through `parse_s3_config` (`backends/s3.py:32`) at write time rather
     than at 02:00; the access key and secret go to `backer/repo/{repo_id}/storage` and are
     assembled back into the `s3` dict the backend expects only when an operation runs.
     `kopia_s3_config` (`backends/s3.py:63`) already keeps them out of argv by emitting
     `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` as environment (`:78-81`), so the serverless
     path reuses it unchanged and adds nothing.
   - Say here, once, that a serverless `local` repository is a directory on THIS client, and is
     not the server's `local` type, which is a directory on the server hardwired to the proxy
     relay (`server/app.py:617-629`, with the repository-type whitelist at `:4610-4611`).
   - This split matters more under Kopia than it did under restic, because `backup` treats ANY
     non-zero connect as "repository absent" and creates a new one (`backends/kopia.py:252-277`).
     A wrong passphrase, an unreachable endpoint and an evaporated mount point all land in that
     branch, so the three outcomes a user most needs told apart end as one green backup over a
     fresh empty repository. Phase 0 item 6 removes the implicit create; this step supplies the
     probe that replaces it.
   - Add `KopiaBackend.repository_probe(destination) -> tuple[str, str | None]` returning one of
     `"present"`, `"absent"`, `"unreachable"`, `"wrong_passphrase"` and, for `present`, the
     repository's unique id. There is no Kopia analogue of the restic-era `repository_id()` at
     HEAD, so this is new code, not a rename. It runs `repository connect <type> <args>
     --no-persist-credentials` followed by `repository status --json`, and classifies on the
     connect stderr, all four strings verified against the pinned 0.23.1 binary:
     `invalid repository password` is `wrong_passphrase`; `repository not initialized in the
     provided storage` is `absent`; `cannot access storage path` or `can't connect to storage` is
     `unreachable`; anything else is `unreachable`. Unrecognised means unreachable, never absent -
     that default is the whole safety property, because only `absent` may lead to a create.
     Neither existing method substitutes: `test_connection` (`:220-228`) returns `True` when the
     repository does not exist, and `list_snapshots` (`:584-624`) returns `[]` for a missing
     password, an unreachable share and an empty repository alike (`:591-592`, `:621-624`).
   - `backer repo add NAME --attach` probes, and proceeds only on `present`. On `absent` it exits
     non-zero naming the resolved path and the fact that nothing is there; on `unreachable` it
     exits non-zero with the verbatim kopia stderr and does not mention creating anything; on
     `wrong_passphrase` it exits non-zero saying the passphrase is wrong for a repository that
     does exist. It never calls `init_repo`. `--init` probes first and proceeds only on `absent`,
     refusing when a repository already answers unless `--adopt` is passed, which routes to the
     adoption step. `--init` becomes `init_repo`'s only client-side caller.
   - Store the probe's unique id - `uniqueIDHex` from `repository status --json`, verified present
     in 0.23.1 - as `unique_id` at add time, and assert it before every run, aborting when it is
     absent or different. A factory reset, a repointed share or a typo'd subpath then fails loudly
     instead of filling a second empty repository. The assertion lives in the `backer.serverless`
     caller, not in `core/runner.py`: server-managed jobs arrive from the heartbeat payload with
     no `unique_id` and must keep running.

4. Passphrase handling (D4), verified before it is stored.
   - Generation, full display, copy-to-clipboard and the explicit save confirmation are A1. A
     user-supplied passphrase arrives through `--passphrase-stdin` or `--passphrase-file FILE`
     only; there is no `--passphrase VALUE`, per protocolfixes.md Phase 0 item 3.
   - Verify before storing, on both `--init` and `--adopt`: put the candidate in `KOPIA_PASSWORD`,
     run the step 3 probe against the resolved destination, and call `keystore.put` only on
     `present`. A typo then fails at setup rather than at 02:00 under a scheduled task, and a
     failed verification leaves no keystore entry behind. This is the one place the
     `wrong_passphrase` classification pays for itself in user-visible terms.
   - Recovery record: a DPAPI blob or a keyring entry is bound to the machine whose death is the
     reason the backup exists, so the passphrase needs one copy that is not on it.
     `--passphrase-out FILE` writes the repository location, the passphrase and the literal
     `kopia repository connect filesystem --path <path>` (or the `s3` form) that opens it; it is
     offered and named per A1, never written to a default path nobody asked for. `backer repo
     passphrase NAME` re-displays it behind a re-confirmation prompt.
   - The passphrase reaches the keystore and nothing else: not `config.yaml`, not the sidecar, not
     a log line, and after the step 2 decision not the machine credential store either.

5. Fatten the sidecar job config. Today the whole `config` object is two keys at
   `client/agent.py:1584-1590` - `{source_path, client_id}` - which reconstructs neither the
   schedule, the excludes, the retention policy nor the owning machine. The document that replaces
   it, at `<repo>/.backer/jobs/{job_subfolder}/config.json`, is Appendix B2's job `config.json`,
   written in place of those two keys and keeping the `{job_name, config, created_at, updated_at}`
   envelope `RepositoryMetadata.save_job` already builds, so the server's importer reads it
   unchanged.
   - `repository_hint` is a hint because the adopting machine may have reached the same bytes
     another way. No absolute destination appears anywhere in the sidecar; each machine recomputes
     it, which is what lets one job run from Windows over a UNC path and from Linux over a mount
     point, and one S3 job run from both with no path at all.
   - No secret ever appears here, and `repository_password_hint` is the single free-text
     exception. Enforce that on the write path: reject any key matching the redaction substring
     list at `agent/service.py:669-686`, and refuse when the hint's value equals a keystore entry
     for that repository, because a field labelled "password hint" beside a passphrase just
     displayed is one somebody will paste the passphrase into. It is a trust boundary, so it fails
     hard.

6. Adoption: `backer repo adopt NAME`.
   - Point a second machine at the repository, take the passphrase and verify it per step 4, then
     call `RepositoryMetadata(root).discover_all()`, which returns per-job sidecars only once the
     early return at `core/repo_metadata.py:585` has been replaced by the Phase 0 merge.
   - Print the discovered jobs - name, source path, schedule, last run, owning hostname - and let
     the user tick which to import; non-interactively `--job NAME` repeated, or `--all`.
   - Adoption is per job, not all-or-nothing: `adopt_documents` collects a failure per job and
     imports the rest, returning `AdoptOutcome(adopted, warnings, failures)`, so one legacy or
     malformed document cannot block every good job. `--all` reads each job's document from
     wherever `discover_all` found it, including the legacy `Agents/{job_subfolder}/.backer/`
     trees, rather than assuming the root sidecar.
   - Adoption refuses rather than guesses, because everything it gets wrong surfaces at 2am on an
     unattended run: an unknown `schema_version` is refused by name (1 and 2 adopt; 1 warns that
     schedule, retention and excludes were never recorded), and an existing local job of the same
     name is refused unless `--replace-existing` is passed - overwriting it repoints a source, the
     same mutation `job set --source` already warns about, and it is warned about here in the same
     words. A source path that does not exist locally, or one recorded on a different platform,
     adopts with a warning naming the required `--source "NAME=<local path>"`; adopting silently
     would schedule a job that can only fail.
   - `enabled` rides in the sidecar document and is honoured on adopt, so a disabled job does not
     wake up on the adopting machine's schedule. `includes`, `pre_scripts` and `post_scripts` are
     not carried and are not reconstructible from the sidecar.
   - Import rewrites machine-local fields only: `source_path`, offering the local equivalent when
     the original does not exist here, and the destination resolution. `job_name` is preserved, so
     the adopted job's sidecar resolves through `get_job_subfolder`
     (`server/repository_paths.py:6-8`) to the same `jobs/` directory. The adopted job's snapshots
     are a distinct Kopia source, because the host component of `user@host:path` differs, which is
     what makes step 9's retention per machine without anyone configuring it.
   - The adopting machine never writes back to `config.json` - one writer, the agent named in
     `owner_agent_id` - only its own `agents/{agent_id}.json` and its own run records.

7. Scheduling with no daemon.
   - `backer job run --due` is the only scheduler entry point and exits 0 when nothing is due;
     `backer job run NAME` is the explicit single-job form.
     `serverless/schedule.py::due_jobs(cfg, now)` evaluates `croniter(job.schedule.cron,
     last_fire).get_next(datetime) <= now`; `croniter` is already a base dependency
     (`pyproject.toml:34`).
   - Do not port `server/scheduler.py`: `_is_job_due` (`:212-243`) matches a one-minute window at
     `:230` and dedupes against an in-memory dict at `:233-237`, so it needs a resident process
     and a restart either re-fires or misses.
   - Persist `<data_dir>/schedule.json`, job name to last fire time in UTC ISO 8601, and record
     the fire time BEFORE the run starts; recording it afterwards leaves a three-hour backup due
     at every tick for three hours. A crash mid-run then skips to the next cron point, which is
     the safe direction, and step 10's attempt record makes it visible.
   - Local run lock `<data_dir>/run.lock`, opened with `os.open(..., O_RDWR | O_CREAT)` and taken
     non-blocking through `fcntl.flock(LOCK_EX | LOCK_NB)` or `msvcrt.locking(LK_NBLCK)`. Do not
     reuse `file_lock` from `core/repo_metadata.py`; it truncates at `:70` before it locks at
     `:74-87` (Phase 0). This lock is a coarser thing than the per-repository kopia mutex of step
     2 and does not replace it: one stops two serverless runs overlapping, the other stops two
     kopia operations sharing a config file.
   - While the run lock is held, `job run --due` prints one line and exits 0 with no attempt
     record, and an explicit `backer job run NAME` exits non-zero naming the holder.
   - The OS triggers are installed by `backer agent install --mode local` in Phase 5, under a task
     and unit name distinct from the server agent's so both modes coexist (D5). This phase owns
     the due calculation, `schedule.json` and the lock.
   - Say what the Linux trigger actually delivers. `create_systemd_service`
     (`client/windows_service.py`) writes a `--user` unit under `~/.config/systemd/user`,
     which systemd does not run while that user is logged out, so a desktop or headless box gets
     no run, no attempt record and nothing to notify about. Phase 5's `--mode local` install
     therefore enables lingering and refuses rather than leaving a timer that never fires; no step
     here promises a scheduled run the trigger will not make.

8. Windows unattended identity and storage credentials (D6).
   - The serverless trigger reuses the existing SYSTEM boot-task machinery under its own name:
     `create_background_scheduled_task` (`client/windows_service.py`), its `S-1-5-18`
     principal and its `MultipleInstancesPolicy` of `IgnoreNew`. The server
     agent's own task is not edited.
   - That task sets `BACKER_DATA_DIR=%ProgramData%\Backer` so the scheduler and the interactive
     user resolve one data directory; without it every job can run twice and `backer job history`
     shows the user nothing the scheduler did. It also fixes the per-repository
     `KOPIA_CONFIG_PATH` and `KOPIA_CACHE_DIRECTORY` under that directory, so the SYSTEM task and
     the interactive user do not each maintain a private connection state for the same repository.
   - Storage credentials are stored with `machine_scope=True` at wizard time under
     `backer/repo/{repo_id}/storage`, which is what makes them readable by SYSTEM at 02:00. That
     applies to the S3 keys exactly as it does to an SMB password; an S3 job is not exempt because
     it never mounts anything.
   - The repository passphrase is scoped the same way, and this is the part that is easiest to get
     wrong: `keystore.put` defaults to `machine_scope=False`, so a passphrase stored by an
     interactive wizard is undecryptable by SYSTEM and every 02:00 run fails at `error_stage:
     keystore` from the first night, visible only to someone who runs `backer status`. Whenever a
     job on a repository is scheduled to run as SYSTEM its `passphrase_ref` and
     `storage_password_ref` are stored machine-scoped.
   - `backer agent install --mode local` enforces that: for every repository the config names it
     re-`put`s both keys with `machine_scope=True`, reads each one back, and exits non-zero naming
     the repository when a value cannot be read at machine scope. `keystore.put` reads its own
     value back before returning wherever it is called, so a repository is never reported created
     against a secret that cannot be retrieved.
   - The rest of this step is SMB only, and unchanged by the move to Kopia because Kopia has no
     SMB provider and reaches a share as a filesystem path (`backends/kopia.py:129-133`). The
     serverless path calls `SMBConnectionManager._connect_with_explicit_credentials`
     (`agent/service.py:277-327`) directly and never `cmdkey`, which stays in the module for
     server-managed mode. `connect` stores through `cmdkey` at `:157-159` then runs a
     credential-free `net use` at `:163-171`, and `cmdkey` entries are per-logon-session, so a
     credential the interactive user added is invisible to SYSTEM.
   - Keep the password off argv: pass `*` as the password argument and write the secret to the
     child's stdin. `agent/service.py:307` passes it as an argument today, which puts it in the
     process list. If a runner's redirector rejects a piped password, fall back to argv and record
     that cell in the Phase 7 matrix rather than pretending otherwise.
   - Error 1219, in this order. Enumerate with `_find_existing_connection`
     (`agent/service.py:222-241`), which shells to `net use` and sees real session state, unlike
     `_find_server_conflict` (`:212-220`), which reads the in-process `_connections` dict and is
     empty in a fresh CLI process. Same server and username: reuse and proceed. Different username
     as SYSTEM: `net use <existing> /delete` and reconnect, which cannot tear down a human's
     mapped drive because the task owns its own logon session. Different username interactively:
     refuse, exit non-zero, and name the conflicting connection and both remedies. Today `connect`
     refuses in every one of those cases and returns `False` after only a `logger.error`
     (`:183-194`). Every refusal writes an attempt record per step 10, because a 1219 that reaches
     only the log is a silent stopped backup. The copy is A2.

9. Retention: delegated to Kopia's own per-source policies, off by default, preview-first.
   - Kopia separates snapshots by source, `user@host:path`, natively. `snapshot list` groups on
     it, `_find_latest_snapshot_for_source` (`backends/kopia.py:386-449`) already filters on it,
     and every `--keep-*` flag is documented "per source" in 0.23.1. So retention is two commands
     against a target, not a tagging scheme: `policy set <user@host:path> --keep-latest N
     --keep-hourly N --keep-daily N --keep-weekly N --keep-monthly N --keep-annual N`, then
     `snapshot expire <user@host:path> --delete`. Verified against the pinned binary: with three
     snapshots each of two sources in one repository and `--keep-latest 1` set on source A only,
     `snapshot expire <A> --delete` reported `Deleted 2 snapshots of matt@host:...\A` and left all
     three of source B's snapshots in place.
   - Per-machine separation is therefore structural, not configured. The host component of the
     source differs between machines, so `keep_last: 7` keeps seven snapshots per machine per
     source with nobody asking for it - which is the safe direction, because a laptop backing up
     weekly must not have its snapshots evicted by a desktop backing up hourly. The restic-era
     two-tag design that bought this property is deleted, not ported.
   - Two jobs on the same folder with different exclude sets are the one case source separation
     cannot cover, and that configuration is already broken for a different reason: the ignore
     policy is per-path (`backends/kopia.py`, `--clear-ignore` then re-add), so two such jobs
     overwrite each other's excludes every run. Note the clear and the re-add must be two separate
     `kopia policy set` invocations: 0.23.1 applies `--clear-ignore` *after* the `--add-ignore`
     values given in the same call, exits 0, and leaves the source with no ignore rules at all -
     which is how 0.9 shipped for a while with every configured exclude silently discarded. Pinned
     end to end by `tests/test_serverless_e2e.py`, which runs a real backup with an exclude and
     asserts the excluded file is absent from the snapshot; argv-shape assertions cannot catch it,
     because the broken form builds argv that looks correct.
     `backer job create` refuses a second job on a source path another job already owns in
     the same repository, and names the owning job. Kopia tags exist and the backend has a dead
     hook for them (`backends/kopia.py:307-309`), but a tag cannot fix the policy collision, so
     the answer is refusal, not tags.
   - `--keep-annual`, not `--keep-yearly`. Verified: `kopia policy set --keep-yearly 3` exits with
     `unknown long flag '--keep-yearly'`. `RetentionConfig` (`core/config.py:24-31`) spells the
     field `keep_yearly` at `:31` and `KopiaBackend.prune` (`:626-634`) has no parameter for it at
     all, so the config field is silently dropped today. Phase 0 adds the parameter and maps it to
     `--keep-annual`; `backer job create` exposes `--keep-last --keep-daily --keep-weekly
     --keep-monthly --keep-yearly` (D7) with the config spelling the user already has, and the
     flag does not ship before that mapping does.
   - Default OFF per job. Every `RetentionConfig` field is `None`, and when every field is `None`
     no `policy set` and no `snapshot expire` runs at all. Retention in the agent is greenfield:
     `grep -rn "\.prune(" src/` returns exactly one caller, `server/app.py:12717`, on the server's
     proxy path, and it passes no keep arguments whatsoever.
   - The S3-versus-SMB split decides how much this matters, and it is not documented anywhere in
     the code. SMB and NFS get one Kopia repository per job today, because
     `_build_backup_command_payload` appends `Agents/{job_subfolder}` to the destination
     (`server/app.py:600`, `:612`). S3 gets none (`:637`), so every job on one S3 repository
     record shares one Kopia repository. Repository-wide expiry is therefore harmless on SMB
     today and destructive on S3 the moment `--delete` lands. The per-source targeting above is
     what makes a shared S3 repository safe, and it must ship before or with the `--delete` fix,
     never after.
   - Preview first, and it is not `--dry-run`. `snapshot expire` has no such flag - appending it
     is the Phase 0 item 2 defect, kopia exits non-zero on the unknown flag - but it does not need
     one, because omitting `--delete` IS the preview. Verified: `snapshot expire <source>` with no
     `--delete` exits 0 and prints `2 snapshot(s) of matt@host:...\src would be deleted. Pass
     --delete to do it.` `serverless/retention.py` parses that count and that source out of
     stdout and reports it; deleting requires `backer prune JOB --apply`, which adds `--delete`
     and nothing else. There is no automatic prune in v1 and no `enforce` field anywhere. Flip the
     default only after the per-source scoping test has been green across a release. The preview
     copy is A3.
   - Refuse to port `server/retention.py`. `apply_retention` (`:81-199`) deletes database rows
     only - its own comment at `:193` says "only affects database records, not actual backup
     files" - and the fall-through at `:189-191` appends every run that reaches it whenever any
     keep policy is set, regardless of the keep set computed above. Serverless retention never
     deletes run records.

10. One unconditional local attempt record.
    - Wrap the serverless call to `run_backup` in `try/finally` that writes exactly one attempt
      record whatever happened, including every pre-flight failure: passphrase missing, keystore
      locked, share unreachable, SMB password rejected, S3 endpoint unreachable, `unique_id`
      mismatch, wrong passphrase, error 1219, kopia binary absent. Fields are the Appendix B2 run
      record plus `repository_id`, and `error_stage` separates a pre-flight failure from a backend
      failure without parsing prose. The step 3 probe outcome is carried verbatim, so "unreachable"
      and "wrong passphrase" are distinguishable in history, not just at the terminal.
    - Generate `run_id` as `{utc_compact}-{agent8}`, replacing the local-time fallback at
      `client/agent.py:871`; a server-supplied `run_id` still wins, which that line already
      prefers.
    - Store through `serverless/store.py::append_run(data_dir, run)` and read back with
      `read_runs(data_dir, job, limit)`, both moved in Phase 1 step 7. Records land under
      `<data_dir>/runs/`, and the newest per job is mirrored to `<data_dir>/last_attempt/` so
      `backer status` answers without walking history. The sidecar copy is already unconditional
      and best-effort after Phase 0; the local record is the one never allowed to be skipped.
    - Progress is coarse and says so. Kopia's `--json` is a result document at completion, not a
      stream, so there is nothing to parse per second and `KopiaBackend.backup` accepts
      `progress_callback` (`backends/kopia.py:235`) without ever calling it. This phase writes
      `<data_dir>/progress/{run_id}.json` at the transitions it actually knows - the run starting,
      the backend coming up, and each kopia stderr frame it can parse - through the
      temp-plus-`os.replace` helper, removed on exit,
      so `backer status` and the desktop client can see a run this process did not start. Phase 5
      owns whichever denominator it picks; this phase claims no percentage it cannot compute.
    - That file stops being a convenience and becomes a contract, because under D8 it is the only
      way an out-of-process client observes an in-flight run. This is the shape the writer actually
      emits (`serverless/runs.py::_write_progress`, fed by `core/runner.py`'s `progress_callback`
      and `backends/kopia.py`'s frame parser), pinned as the contract:
      - `run_id` is on every frame, and is the only key that is.
      - `status`, not `phase`: `"started"` on the first frame, then `"running"` for the rest. There
        is no terminal frame - the document is deleted when the run ends, and its absence is what a
        reader treats as "not running". The four-value `phase` enum (`started`, `estimated`,
        `complete`, `failed`) was never implemented and is not the contract.
      - The `"started"` frame carries `started_at`, UTC ISO 8601 with a trailing `Z`.
      - `"running"` frames carry `message` and `progress_percent` at the coarse transitions the
        runner knows, and `current_file`, `bytes_processed`, `files_processed`, `total_bytes`,
        `total_files`, `hashed_bytes`, `cached_bytes`, `hashed_files`, `cached_files` once kopia's
        stderr frames start arriving. The key set is therefore not stable between frames: a reader
        binds every field as optional and holds its last known value.
      - There is no `job` key and no `updated_at`; the run id names the file and the file's mtime
        is the update time.
      - `total_bytes` is an *estimate*: the previous snapshot's `rootEntry.summ.size`. `null` means
        "no denominator", and `_clamp_progress` (`serverless/runs.py`) sets both `total_bytes` and
        `progress_percent` to `null` as soon as `bytes_processed` exceeds it, so a grown source
        reports indeterminate progress instead of a false percentage; while the estimate still
        holds, `progress_percent` is capped at 99 so a bar never sits full mid-run. `total_files`
        is `0` rather than `null` when unknown, which is the one place the "null, never 0" rule is
        not kept - a reader must treat `total_files == 0` as unknown.
      - Writes are whole-file temp-plus-`os.replace`, so a reader polling it never sees a partial
        document. Adding a field is backward compatible; renaming or repurposing one is a break and
        belongs in CHANGELOG.md.
      - Both halves are pinned by fixtures generated from the real writer:
        `tests/test_metadata_fixtures.py` fails if the Python writer's bytes change, and
        `desktop/Backer.Desktop.Tests/MetadataFixtureTests.cs` fails if the C# reader stops binding
        them. The C# `ProgressFrame` deliberately does not bind `message`, `progress_percent` or
        `current_file`: the desktop client composes its own status line and draws its bar from
        `bytes_processed`/`total_bytes`, so binding them would add unread fields.
    - Readers: `backer job history NAME`, `backer status --exit-code`, and the desktop client,
      which reads the same on-disk records directly rather than importing `read_runs` (D8).
      `--exit-code` exits non-zero when any enabled job's newest attempt failed or its last success
      is older than two of that job's own schedule intervals. That flag turns a silently stopped
      nightly job into something a monitoring check can see.
    - Fix the log path per A5 item 4: `setup_agent_logging` (`agent/service.py:55-59`) logs under
      `%ProgramData%\Backer\logs` when frozen or running as a service, granting Users read, and
      under `$XDG_STATE_HOME/backer/logs` on Linux. Today it writes `%APPDATA%` (`:57`), which
      under SYSTEM is a directory the interactive user cannot find.
    - Read Last Run Time and Last Result from `get_background_task_status`
      (`client/windows_service.py:337`) so a run that never started is reported per A5 item 6; an
      attempt record cannot describe a trigger that did not fire.

Acceptance:

- Every test named below is new. `python -m pytest -q tests/test_keystore.py
  tests/test_serverless.py` exits 0 on both Linux and Windows.
- `tests/test_keystore.py::test_put_get_delete_roundtrip` passes against DPAPI on Windows, and
  against Secret Service on Linux only when `DBUS_SESSION_BUS_ADDRESS` is set and
  `shutil.which("secret-tool")` resolves, calling `pytest.skip` otherwise the way
  `tests/test_s3.py:226-234` does. On both CI runners that skip is expected, and the leg that runs
  is `::test_headless_fallback_reports_file_backend`, asserting `put` returns `"file"`, the blob
  is mode `0600` and its directory `0700`.
- `tests/test_serverless.py::test_probe_distinguishes_absent_unreachable_and_wrong_passphrase` -
  three fixtures against a real kopia binary, an empty directory, a path whose parent does not
  exist and a real repository opened with the wrong passphrase, asserting `repository_probe`
  returns `"absent"`, `"unreachable"` and `"wrong_passphrase"`, and that an unrecognised stderr
  maps to `"unreachable"`.
- `::test_attach_refuses_when_no_repository` - `repo add --attach` against an empty directory
  exits non-zero and the subprocess spy records no `repository create`;
  `::test_init_refuses_existing_repository_without_adopt` exits non-zero where the same command
  with `--adopt` exits 0; `::test_unreachable_destination_never_creates_a_repository` points
  `--init` at a path under a missing parent and asserts a non-zero exit and no `repository create`
  in the recorded argv.
- `::test_unique_id_mismatch_aborts_before_backup` repoints a job at a second freshly created
  repository and asserts a non-zero exit, zero snapshots there, and one attempt record with
  `error_stage == "prepare_destination"`; `::test_server_managed_job_without_unique_id_still_runs`
  asserts a job dict with no `unique_id` reaches `run_backup` unblocked.
- `::test_no_kopia_command_persists_credentials` - every recorded kopia argv for a serverless
  operation carries `--no-persist-credentials` on `repository create` and `repository connect` and
  `--use-credential-manager` on none of them; `::test_runner_disconnects_before_connecting`
  asserts a `repository disconnect` against the repository's own `KOPIA_CONFIG_PATH` precedes the
  connect and that its failure is ignored.
- `::test_wrong_passphrase_fails_at_setup` - attach with a wrong passphrase exits non-zero and
  `keystore.get("backer/repo/<id>/passphrase")` returns `None`;
  `::test_s3_keys_live_only_in_the_keystore` - after `repo add` for an `s3` record, `config.yaml`
  contains neither key, the record carries `bucket`, `prefix`, `endpoint`, `region` and
  `path_style`, and the recorded kopia argv carries no key while the child environment carries
  both; `::test_job_config_carries_no_secret` - the job document carries `"schema_version": "2"`,
  no value equal to the passphrase, the SMB password or either S3 key, and no redaction-list key
  except `repository_password_hint`, and a hint equal to the stored passphrase raises rather than
  writing.
- `::test_fire_time_recorded_before_run` - a job whose backup runs past its next cron point is not
  returned by a second `due_jobs()` call and `schedule.json`'s last fire time is at or before the
  run start; `::test_run_due_exits_zero_when_lock_held` asserts one printed line, exit 0 and no
  attempt record; `::test_system_task_and_interactive_user_resolve_the_same_run_lock` asserts
  `get_data_dir()` returns `BACKER_DATA_DIR` under both identities and the two `run.lock` paths
  compare equal.
- `::test_retention_is_scoped_to_one_source` - two jobs on two source paths in one repository at
  `keep_last=1`, retention run on job A: the emitted argv is `policy set <A source> --keep-latest
  1 ...` and `snapshot expire <A source>` with no `--delete` and no `--dry-run`, and job B's
  snapshot ids are unchanged; `::test_apply_adds_delete_and_nothing_else` asserts `backer prune JOB
  --apply` emits the same argv plus `--delete`; `::test_preview_reports_the_count_kopia_printed`
  parses `2 snapshot(s) of ... would be deleted` and asserts the reported count is 2;
  `::test_keep_yearly_maps_to_keep_annual` asserts a `RetentionConfig` with `keep_yearly=3` emits
  `--keep-annual 3` and never `--keep-yearly`, which kopia 0.23.1 rejects.
- `::test_second_job_on_the_same_source_is_refused` - `backer job create` for a source path
  another job already owns in the same repository exits non-zero and names the owning job.
- `::test_install_local_rescopes_secrets_to_machine` - after `backer agent install --mode local`
  every key the config references returns its value under `machine_scope=True`, and a key that
  cannot be read at machine scope fails the install with a non-zero exit naming the repository
  rather than failing the 02:00 run.
- `::test_status_exit_code_flags_a_stale_job` - `backer status --exit-code` exits 0 after a fresh
  success, non-zero when the newest attempt has `status == "failed"`, and non-zero when the last
  success is older than two of that job's own cron intervals with the share offline, reading only
  `<data_dir>/last_attempt/`.
- `::test_preflight_failure_writes_attempt_record` - an unreachable share produces exactly one
  attempt record with `status == "failed"` and `error_stage == "prepare_destination"`, and `backer
  job history NAME` prints it.
- `tests/test_agent_service.py::test_serverless_smb_connect_skips_cmdkey` - the recorded argv for
  a serverless connect contains no `cmdkey` while the server-managed path still calls it;
  `::test_error_1219_as_system_reclaims_connection` - the SYSTEM branch issues `net use <existing>
  /delete` and retries, the interactive branch exits non-zero without deleting, and both write an
  attempt record.
- The adoption test, `tests/test_serverless.py::test_second_machine_adopts_and_appends_snapshot`:
  run a job from config A against a repository, delete config A and its entire local data
  directory, build config B holding only the repository coordinates plus the passphrase, run
  `backer repo adopt` and then `backer job run`, and assert `kopia snapshot list --all` returns
  two snapshots in the one repository at the repository root under two different `user@host`
  sources, and that `.backer/jobs/{job_subfolder}/runs/` holds two records written under two
  different agent ids.
- The twelve clean-restore rollback tests at `tests/test_agent_protocol.py:198-546` still pass,
  as amended by the Phase 0 fix to `client/agent.py:1286-1294`; this phase does not edit them.

## Phase 5 - CLI and setup wizard

Implements D7. No new dependency: `click`, `rich`, `pydantic`, `pyyaml` and `croniter` are already
base dependencies (`pyproject.toml:29-36`). The interactive surface is a new
`src/backer/client/serverless_wizard.py` on the rich primitives `client/setup_wizard.py` already
uses; the non-interactive surface is `src/backer/cli.py`. New tests land in
`tests/test_cli_serverless.py` unless a bullet names another file.

**Status.** Mostly on disk. `backer --help` lists `init`, `repo`, `job`, `schedule`, `keystore`,
`snapshots`, `status`, `prune`, `verify`, `restore` beside the pre-existing `agent`, `server`,
`setup`, `backup` and `tools`; `src/backer/client/serverless_wizard.py` is the `init` wizard;
`agent install --mode local|server`, `agent uninstall --service-only`, `agent test-schedule`,
`job set`, `schedule pause --until|resume|show|status` and `keystore status --json` all resolve.
Tests live in `tests/test_cli_serverless.py`, `tests/test_serverless_wizard.py`,
`tests/test_serverless_modes.py` and `tests/test_serverless_unattended.py`. Not verified here: the
`--json` completeness audit in step 2, and the Windows-only legs of the acceptance bullets.

1. Extend the existing groups. There is no `backer standalone` group: D5 makes the mode a flag, and
   a parallel group makes it a product boundary. `backer job` already takes `--server` on every
   subcommand (`cli.py:1377`, `:1433`, `:1480`), so that flag is the mode selector - `--repo NAME`
   resolves the job from the local config, `--server URL` posts to the server as today, both
   together is a usage error, neither falls back to the unified config. Delete "(requires server)"
   from the `job` group docstring at `cli.py:1372`. The only new group is `backer repo`, because
   repositories have no CLI surface today.
   - `backer setup` (`cli.py:33-36`) keeps meaning "download Kopia", `backer agent setup`
     (`cli.py:588`) keeps meaning server enrolment, `backer init` is the serverless front door. Say
     which is which in all three help texts so they stop reading as synonyms.
   - `backer agent install` gains `--mode local|server`, orthogonal to the existing `--method`
     Choice at `cli.py:756`. Under `--mode local` it skips the `BackerAgent.from_config()`
     registration check at `cli.py:783-787`, which today aborts with `Agent not registered` on a
     machine that has no server, and installs under a distinct name so both modes coexist (D5):
     `create_background_scheduled_task` (`client/windows_service.py`) as `BackerLocalSchedule`,
     `create_systemd_service` (same file) as `backer-local.timer`, both with an action of `backer job
     run --due --no-progress`. On a frozen install the task's action is `backer.exe`, not the
     interpreter.
   - `--mode local` routes through the full freeze-verify-rollback path rather than the no-rollback
     subset it used to be. That sequence - freeze the running scheduler, re-scope every referenced
     keystore entry to machine scope, write both the per-user and the machine-scoped `config.yaml`,
     read every secret back at machine scope, and roll back the scheduler, the secrets and both
     config files on any failure - lived in the deleted GUI and is the only implementation that was
     ever correct. It moves into the CLI, because under D8 a UI cannot own an install transaction:
     the process that performs it must also be the process that unwinds it, and that process is
     `backer`. Phase 4 step 8's machine-scope enforcement is the middle of this sequence, not a
     second copy of it.
   - `create_systemd_service` (`client/windows_service.py`) writes a `--user` unit under
     `~/.config/systemd/user`, which systemd does not run while that user is logged out.
     `--mode local` on Linux therefore also runs `loginctl enable-linger <user>`, verifies it with
     `loginctl show-user <user> --property=Linger`, and exits non-zero naming that command when it
     cannot be enabled - an installed timer that never fires is worse than a refused install. The
     install output names the identity the timer runs as and the config file that identity resolves.

2. This is the whole command surface. No other section introduces a command or a flag; anything
   another section needs is here, spelled exactly as here. There is no `--backend`: Kopia is the
   only engine (`backends/registry.py:9-12` registers KOPIA and PROXY, and PROXY is server-managed),
   so a choice with one member is a prompt that teaches nothing.

   ```
   backer init                 the wizard; every step below is also a flag
   backer repo add NAME        --attach | --init (exactly one is required)
                               --adopt (with --init, when a repository already
                               answers at that path)
                               --type local|smb|s3
                               local:  --path PATH
                               smb:    --server/--host --share --path --username
                                       --domain
                                       --storage-stdin/--password-stdin |
                                       --storage-file/--password-file FILE
                               s3:     --bucket --prefix --endpoint URL --region
                                       --path-style --access-key-id
                                       --secret-key-stdin | --secret-key-file FILE
                                       (the same --storage-stdin/--storage-file
                                       carry the S3 credentials JSON)
                               --generate-passphrase [--passphrase-out FILE |
                               --print-passphrase] | --passphrase-stdin |
                               --passphrase-file FILE
                               --headless
   backer repo list            --json
   backer repo test|unlock NAME
   backer repo passphrase NAME reprints the words and the save options
   backer repo rm NAME         --yes --passphrase-out FILE
                               (the record and this computer's keystore entries;
                               the data is left on the share, and nothing can
                               read it afterwards - A1 item 8)
   backer repo discover        --host --username --password-stdin --json
   backer repo adopt NAME      --job NAME (rep.) --all --json
   backer repo recover NAME    --passphrase-stdin | --passphrase-file FILE
                               (A7: give an existing repository a passphrase)
   backer job create NAME      --repo | --server --source (rep.) --exclude (rep.)
                               --schedule CRON | daily@HH:MM | --no-schedule
                               --keep-last --keep-daily --keep-weekly
                               --keep-monthly --keep-yearly
   backer job run NAME|--all|--due    --repo | --server --dry-run
                               --progress/--no-progress
   backer job list             --repo | --server --json
   backer job show NAME        --repo | --server --last --verbose --json
   backer job history NAME     --repo | --server --limit N --json
   backer job rm NAME          --repo | --server --yes
   backer snapshots JOB|--repo NAME   --host NAME --source PATH --limit N --all
                               --json
   backer restore --job NAME   --into NEW|MERGE|REPLACE --destination PATH
                               --snapshot ID --before WHEN --latest
                               --include SUBPATH --computer NAME --yes-replace
                               --dry-run --clean-up-replaced
   backer restore --from PATH  the same flags, with no prior configuration (A4)
   backer prune JOB            --list --apply --yes-remove N --json
   backer verify JOB           --restore-test --verify-files-percent N
                               --repair-index --timeout N
   backer status [JOB]         --why --exit-code --json
   backer agent install        --mode local|server
                               --method service|task|startup|systemd
   backer agent uninstall      --mode local|server --headless --keep-config --yes
                               --service-only (removes only the installed
                               service/task/unit; config, data, keystore and
                               binaries are left in place - this is what the
                               desktop client's Remove agent service shells)
   backer agent test-schedule [NAME]
                               runs one job the way the scheduler would, as
                               SYSTEM, and prints what it saw; exit 0 or 1
   backer job set NAME         --schedule CRON | --no-schedule
                               --keep-last|-daily|-weekly|-monthly|-yearly N
                               --exclude P (rep.) --clear-excludes
                               --enable | --disable --source P --json
   backer schedule pause       --until ISO8601
   backer schedule resume
   backer schedule show        --json
   backer schedule status      --json
   backer keystore status      --json
   ```

   - Exit codes are the scripting contract and are tested: 0 success, 1 the operation failed, 2
     configuration or usage error, 130 interrupted.
   - The last block is the surface D8 needs and the CLI did not have. Each entry exists because a
     screen would otherwise have had to mutate state itself, which D8 forbids.
     - `job set` is edit. Without it the only way to change a schedule, a retention field, an
       exclude list or an enabled flag was to rewrite `config.yaml` in place - which is exactly
       what the deleted GUI did, and exactly the second writer D8 removes. It carries `--json` so
       the client can read back what it wrote instead of guessing.
     - `schedule pause|resume|show` own the tray's Pause backups control. State stays in
       `<data_dir>/schedule-runtime.json` beside `schedule.json`; `show --json` emits
       `{"paused": bool, "until": str|null}`. Pause suppresses `job run --due` and nothing else; it
       never removes an OS trigger, because a trigger that stops existing is a trigger nobody
       notices is gone.
     - `schedule status --json` wraps `windows_service.snapshot_local_scheduler` plus
       `local_schedule_configured` and reports honestly what those actually provide -
       `configured`, `method`, `scope`, `enabled`, `active` - rather than a synthesised boolean. No
       CLI could read scheduler state before this.
     - `keystore status --json` returns `{"backend": str, "file_fallback": bool}`: a backend name,
       never a secret, which is all a UI needs to say "stored in DPAPI (user)" or to repeat A1's
       downgraded-store warning (D4).
     - `backer agent test-schedule [NAME]` is the user-facing self-test and the one the desktop
       client's "test a scheduled run now" control spawns. It runs the sequence the moved helpers in
       `serverless/scheduled_test.py` already implement - `prepare_scheduled_test`, create the
       transient SYSTEM task or systemd unit, `wait_for_scheduled_attempt`, `remove_scheduled_test`
       with `retry_scheduled_test_cleanup` on failure - prints human output and exits 0 or 1.
       Cleanup is fail-closed: the isolated machine-scope credentials are deleted only after removal
       is verified, and a failed cleanup is reported rather than swallowed.
     - `backer agent scheduled-test TOKEN` is hidden and takes the place of `python -m
       backer.serverless.scheduled_test TOKEN` on a frozen install, where there is no interpreter
       to invoke. It is the inner leg of D6's privileged self-test and is not a user-facing command.
   - Confirmations gain non-TTY forms and stay fail-closed. `restore --into REPLACE --yes-replace`
     works with no TTY because the flag itself carries the confirmation; without the flag, a
     non-TTY invocation still refuses with exit 2. `repo rm --yes --confirm-name NAME` replaces the
     typed-name prompt with the name supplied as a value that must match. `verify --repair-index
     --yes`. Nothing here weakens the interactive default: on a TTY with no flag, every one of them
     still prompts exactly as A1, A3 and A4 specify. This is D7's "every prompt is also a flag"
     completed for the three prompts that had no flag, which is what lets D8's client drive them.
   - `--json` completeness is a gate on this phase, not a nicety, because it is the client's only
     read path for anything not on disk. Audit the whole table before the desktop work starts:
     `repo list`, `repo adopt`, `repo discover`, `job list`, `job show`, `job history`, `prune`,
     `snapshots` and `status` carry it today; `repo test`, `repo unlock`, `repo passphrase`,
     `verify` and `restore` do not, and each either gains `--json` or is documented here as
     human-output-only with the client showing that output verbatim. `job run NAME --json` is
     special and is specified in step 6.
   - No secret is ever an argv value: `--password-stdin`/`--passphrase-stdin`/`--secret-key-stdin`,
     their `--*-file` forms, and the `$BACKER_SMB_PASSWORD` / `$BACKER_REPOSITORY_PASSWORD` /
     `$BACKER_S3_SECRET_KEY` fallbacks through the existing `envvar=` pattern (`cli.py:985-986`).
     `BACKER_REPOSITORY_PASSWORD` is the name the existing `backer backup`/`backer restore` commands
     already use, so serverless mode adds no second spelling. That is also why `repo discover` never
     puts a password on a command line (protocolfixes.md Phase 0 item 3).
   - The S3 flags are exactly the fields `parse_s3_config` requires (`backends/s3.py:32-60`):
     bucket, prefix, endpoint, region, access key id, secret. There is no `--no-tls` flag - an
     `http://` endpoint emits `--disable-tls` on its own (`backends/s3.py:73-74`), so the transport
     cannot disagree with the URL the user typed.
   - The five `--keep-*` flags are the five fields of `RetentionConfig` (`core/config.py:24-31`), so
     the CLI keeps the name the config file uses. `--keep-yearly` reaches the engine as kopia's
     `--keep-annual`, which is the flag's actual spelling; Phase 0 owns that mapping and the
     `keep_yearly` parameter `KopiaBackend.prune` does not have today (`backends/kopia.py:626-634`).
     No flag here is exposed before that lands.
   - `--snapshot` takes a kopia snapshot id and nothing else. It does not accept `latest`: `kopia
     snapshot restore latest <dir>` is not a valid reference, which is why `backer restore` without
     `--snapshot` fails outright today (`backends/kopia.py:502-517`). `--latest` and `--before WHEN`
     resolve to a real id through `_find_latest_snapshot_for_source` (`backends/kopia.py:386`)
     before the restore runs, so every restore names one immutable id.
   - `--headless` is the passphrase flow's non-interactive form: it suppresses the A1 screen
     entirely, so the passphrase must arrive by `--passphrase-stdin`, `--passphrase-file`, or
     `--generate-passphrase` with `--passphrase-out` or `--print-passphrase`, nothing is cleared
     from the screen and nothing is confirmed by position. Without it, `repo add` off a TTY exits 2
     per step 4 rather than silently skipping the confirmation.
   - `backer repo recover` is A7's command. It is the only production caller of
     `Storage.set_repository_password` (`server/storage.py:926`), which has none today, and it is
     what turns a repository created before the passphrase became mandatory back into one that can
     be opened.
   - `backer repo rm` is the only command that destroys the ability to read data it leaves in place,
     because the keystore holds the only copy of the passphrase. Its confirmation is the repository
     name typed back, `--yes` does not bypass the warning, and the copy is A1 item 8.
   - `backer prune` always previews (A3), `--apply` is the only thing that deletes snapshots, and no
     job run ever prunes in v1.
   - `backer repo discover` enumerates shares and directories on a host the user named. There is no
     subnet sweep and no `--scan`: `client/setup_wizard.py:166` already records that finding hosts
     was deliberately skipped, and that stays true.

3. Every prompt is a flag, and the wizard is a flag-filler rather than a second implementation.
   `backer init` collects answers into the same parameter dict that `repo add`, `job create` and
   `agent install` build from their own flags, then calls the exact functions those commands call.
   One `steps` table of `(key, flag, prompt, validator)` is shared by the wizard and by the
   missing-flag reporter so the two cannot drift, a step with no flag is a bug the first Acceptance
   test catches, and the wizard closes by printing the non-interactive command it just ran, rendered
   from that same dict - pasteable into a runbook and the cheapest available proof that the flag
   surface is complete.

4. No TTY means no prompt, ever. One `_interactive()` helper (`sys.stdin.isatty() and
   sys.stdout.isatty()`) gates every prompt. Each command resolves its parameters from flags,
   environment and config, then asks once whether anything is unset: on a TTY it prompts, off a TTY
   it prints one message naming every missing flag plus the reconstructed command and exits 2. rich
   needs no configuration for this - `Console` already drops ANSI when stdout is not a terminal. Two
   refusals earn their code: `--generate-passphrase` off a TTY with neither `--passphrase-out` nor
   `--print-passphrase` exits 2, because a generated passphrase nobody ever sees is silent data
   loss, and `--into REPLACE` without `--yes-replace` exits 2 rather than moving live files aside.

5. Build the share-and-folder browser as a re-rendered numbered table, not `rich.Live`. Live owns
   the terminal and fights `Prompt.ask`; Live is reserved for the backup itself, where nothing is
   being typed. Each keystroke prints a fresh `rich.Table` with no `console.clear()`, so history
   stays in scrollback and the picker works over a dumb SSH pipe.
   - One `list_dir(path) -> list[Entry]` callable with exactly two implementations, because there
     are exactly two callers: `os.scandir` for local, `backer.core.smb_browse` for shares. S3 has no
     browser and needs none - a bucket and a prefix are two typed fields, which is why S3 is the
     cheapest of the six cells rather than the most expensive.
   - Keys: number descends, `u` up, `Enter` accepts the current folder, `n` creates one (reject path
     separators, `..`, a leading dot, over 255 characters), `/text` filters the listing, `m` pages
     by 20, `q` aborts with exit 130 having written nothing. Directories are marked with a trailing
     `/` and a Type column, never colour alone.
   - Wrap every SMB round trip in the transient `Progress(SpinnerColumn(), TextColumn(...))` already
     at `client/setup_wizard.py:181-186`, labelled specifically ("Listing shares on nas.local"), so
     a NAS spinning up its disks looks busy rather than hung. The browser never appears
     non-interactively: `--share` and `--path` supply it, and a missing `--path` off a TTY is exit
     2.

6. Rebuild live progress on kopia's terms, then render it. Kopia has no NDJSON stream: `snapshot
   create --json` is one result document written to stdout at completion, which is why the
   line-by-line parser at `backends/kopia.py:328-347` fires exactly once, and `--progress` is
   human-readable stderr. Three mechanisms were available - a `snapshot estimate` denominator with
   an indeterminate bar, stderr scraping, and kopia's server mode. **Scrape `--progress` stderr.**
   - It works off a terminal, which is the fact the whole choice turns on. Verified against the
     pinned 0.23.1 binary (`tools/manager.py:21-37`) with stderr on a pipe: `snapshot create --json
     --progress` still emits frames of the form ` * 0 hashing, 60 hashed (720 MB), 0 cached (0 B),
     uploaded 713.2 MB, estimating...`, about one every three seconds. They are separated by `\r`,
     not `\n`, so `for line in proc.stderr` blocks until the command ends; the reader must split on
     both. Kopia needs no `RESTIC_PROGRESS_FPS` equivalent and none exists.
   - The denominator is free and is not `snapshot estimate`. `snapshot list --json <source>` carries
     `stats.totalSize` for the previous snapshot of that exact source, `list_snapshots` already
     surfaces it as `size` (`backends/kopia.py:614`), and `_find_latest_snapshot_for_source`
     (`backends/kopia.py:386`) already fetches that listing. Use the previous snapshot's size as the
     denominator and `hashed + cached` from the frame as the numerator. A source with no previous
     snapshot has no denominator and stays indeterminate for its whole first run, which is honest
     and costs nothing.
   - Restore is determinate, and better than restic's was. `snapshot restore --progress` writes
     newline-delimited `Processed 17 (216 MB) of 60 (720 MB).` to stderr - both numbers, from kopia,
     needing no denominator of ours. Convert `backends/kopia.py:540-546` from
     `subprocess.run(capture_output=True)` to the same `Popen` reader and drop the old claim that
     restore can only show elapsed time.
   - The cost, stated: the frame format is undocumented and pinned to 0.23.1, so a kopia bump can
     silently turn the bar indeterminate. One regression test replaying a recorded frame bounds it;
     nothing bounds it if the format is trusted silently. Kopia's own estimator reports
     `estimating...` for short runs, so there is no ETA to surface and none is invented. Rejected:
     `snapshot estimate` has no `--json` (verified) and walks the whole source tree a second time
     before every backup for a number the previous snapshot already gives; kopia's server mode means
     a daemon, a port, a TLS certificate and an auth story for a one-shot CLI invocation.
   - Backend changes: add `--progress` to the argv built at `backends/kopia.py:281-311`, convert
     `backends/kopia.py:313-319` to `Popen(stderr=PIPE, text=True)` with a reader thread, invoke
     `progress_callback` per frame, and leave the stdout result-document handling at
     `backends/kopia.py:328-347` untouched - it is correct, it just runs at the end.
   - Fix the capability lie in the same commit that first renders a bar. `client/agent.py:954-957`
     decides a backend is progress-capable by looking for `progress_callback` in
     `backend.backup.__code__.co_varnames`, and Kopia declares the parameter and ignores it, so the
     check returns True today and would tell any new UI the same. Delete those four lines and pass
     the callback unconditionally at `client/agent.py:964`. A backend that never calls it produces
     no frames, and the next bullet already covers that case; capability is learned from frames
     arriving, not from a signature.
   - Never draw a percentage without a frame behind it. With no frame for 45s the CLI prints `no
     progress for 45s` under the bar and keeps the last figures, so a stalled transfer looks
     different from a slow one. Reuse the renderer already shipped for `backer agent progress` -
     `format_bytes` (`cli.py:1054`), `create_display` (`cli.py:1082`), the Live loop
     (`cli.py:1153-1162`) - with the HTTP poll replaced by the local callback.
   - `--progress` forces Live, `--no-progress` forces one timestamped line per update, the default
     is Live only on a TTY. Ctrl-C sends SIGINT (CTRL_BREAK on Windows) to kopia, waits up to 30s
     for it to finish writing and disconnect, then exits 130.
   - `backer job run NAME --json` is how an out-of-process client attaches to a run, and its output
     contract is exact. The **first** stdout line is `{"run_id": "..."}`, written and flushed
     **before** the run starts - before the keystore read, before prepare_destination, before kopia
     is spawned - so a caller always has an id to correlate with even for a run that fails in its
     first second. The final stdout line is one JSON result object. Under `--json` there is nothing
     between them: the `[BACKUP]`/`[METADATA]` narration goes to stderr (`_json_only_stdout` in
     `cli.py`), so stdout is exactly two JSON documents and `jq` on it succeeds. `--json` implies
     `--no-progress`.
   - Progress transport for the desktop client, and this is the whole of it: the client reads
     `<data_dir>/progress/{run_id}.json` at about 4 Hz and tails `<data_dir>/logs/{run_id}.log`,
     using the `run_id` from that first line. That is Phase 4's coarse document - one `started`
     frame, then `running` frames - so the client's bar is coarse too, and Phase 6 step 8's rule
     bites accordingly: with no per-frame data there is no percentage to draw, and the view says
     what it is doing instead of inventing a number.
   - **Per-frame NDJSON progress is deliberately deferred.** Adding `--progress-json` to `job run`,
     emitting one NDJSON object per parsed kopia frame on stdout, is maybe fifty lines on top of the
     stderr reader this step already builds - the frames are already parsed. It is deferred because
     it buys a smoother bar and nothing else, and because a stream is a second output contract to
     version. Trigger to build it: the coarse bar being the reported problem - users asking whether
     a long first backup is stalled - or a Phase 6 view that needs a throughput figure it cannot
     compute from three transitions. When that happens it is additive: the same first-line `run_id`
     contract, the same result line, one new flag.

7. The `Choice()` list is the support matrix. `--type` accepts `local|smb|s3` on both platforms, per
   D2, which is the six cells. Widening the matrix and widening a `Choice()` are the same commit as
   the passing end-to-end cell from Phase 7, and Phase 7's workflow test fails a `Choice` entry that
   has no green job. `s3` is in the list from the first commit because its cell already has a
   passing job (`tests/test_s3.py:226`, `.gitea/workflows/release-validation.yml:77-114`).

8. Surface the error strings the code already has; do not collapse them into `Error: {e}` the way
   `backer backup` does at `cli.py:189-191`. Specific messages already exist for SMB permission
   denied, share not found and host down (`client/agent.py:642-649`), for missing cifs-utils with
   per-distro install commands (`:595-600`), and for Windows error 1219
   (`agent/service.py:183-194`). Every failure panel names the machine, the share, the account and
   the next command, and says
   whether anything was written; the wording is Appendix A.
   - Name the conflicting connection from `_find_existing_connection` (`agent/service.py:222-241`),
     which shells to `net use` and sees real session state. Never `_find_server_conflict`
     (`:212-220`): it reads the in-process `_connections` dict, which is empty in a fresh CLI
     process.
   - `backer snapshots` must not print "no snapshots" on an empty list.
     `KopiaBackend.list_snapshots` returns `[]` for a failed connect (`backends/kopia.py:591-592`)
     and again for any exception (`:621-624`), so a wrong passphrase, an unreachable NAS, a missing
     passphrase and a genuinely empty repository are indistinguishable. On empty, call the
     `repository status` probe Phase 4 adds and print its verdict. Do **not** call
     `KopiaBackend.test_connection`: it returns `(True, "...will be initialized on first backup")`
     whenever the connect error contains `not initialized` (`backends/kopia.py:226-227`), which is
     exactly what kopia says for a reachable directory holding no repository, so it reports success
     for the one case that must be reported as a problem. One branch in the CLI, no backend change.
   - Two `verify` behaviours need backend code that does not exist. Phase 0 replaces
     `KopiaBackend.check`'s `repository validate-client` (`backends/kopia.py:771`) - which is not a
     kopia 0.23.1 command - with `snapshot verify`; Phase 5 adds the two arguments the CLI needs on
     top of that fix: a `verify_files_percent: float | None` parameter emitting
     `--verify-files-percent`, and `KopiaBackend.repair_index(destination, commit: bool = False)`
     running `kopia index recover [--commit]`. Both reuse the existing
     `self.config.get("timeout", 3600)` handling (`backends/kopia.py:775`) and the same
     `(TimeoutExpired, OSError, RuntimeError)` tuple (`:789`).
   - `backer restore --job NAME` passes `original_source_path` (`backends/kopia.py:457`) through the
     call the Phase 0 fix repaired at `cli.py:224`. `--include` maps to the `include_path` parameter
     (`backends/kopia.py:458`); there is no kopia equivalent of restic's in-place include helper, so
     Phase 5 writes the subpath validation itself - reject absolute paths, reject any `..`
     component, reject a drive letter - and test it directly. `backer
     status`, `job show` and `job history` read `backer.serverless.store.read_runs` - no new store,
     no daemon.
   - Create `src/backer/core/messages.py`, the Appendix A catalogue plus A5's substring map. The CLI
     imports it here and it stays the only copy: the desktop client displays the CLI's stdout and
     stderr verbatim rather than holding strings of its own (D8), so the no-drift guarantee holds by
     construction rather than by an export step and a conformance test.
   - Implement A4's moved-aside copy: `client/agent.py:1398` renames to
     `<name>.replaced-<timestamp>` instead of `shutil.rmtree`, which survives only behind
     `--clean-up-replaced`.
   - Implement A4's destination deny list, widening the root-only guard at
     `client/agent.py:1307-1308` and checking it for all three `--into` modes rather than only the
     clean-restore branch it sits in today.

9. One representative session, first-time setup against a NAS:

   ```
   $ backer init
   Step 1 of 5  Where backups are stored
     1  A local disk or external drive     2  A network share (SMB)
     3  S3-compatible object storage
   Choice [1/2/3] (2): 2

   Step 2 of 5  Sign in to the file server
   Server name or address: nas.local
   Username: matt        Password: ********
     Signed in. 4 shares available.
   Shares on nas.local          //nas.local/Backups/
    1  Backups                   1  Archive/   2025-11-02
    2  Media                     2  Laptops/   2026-01-14
    [number] open  [u] up  [Enter] use this folder  [n] new folder  [q] quit
   > 1   > 2   > n
   Folder name: matt-laptop
     Created //nas.local/Backups/Laptops/matt-laptop

   Step 3 of 5  Encryption passphrase        (the screen and its copy are A1)
   Step 4 of 5  What to back up
   Folder to back up (Enter to browse): C:\Users\matt\Documents
   Skip anything? comma separated (Enter for none): node_modules

   Step 5 of 5  Schedule
     1  Every day at 02:00   2  Every hour   3  Only when I run it   4  Cron
   Choice [1/2/3/4] (1): 1
   Name for this job (documents):

   ╭─ Ready ────────────────────────────────────────────────────╮
   │ Job  documents        Schedule  every day at 02:00         │
   │ Back up         C:\Users\matt\Documents  skip node_modules │
   │ Repository      //nas.local/Backups/Laptops/matt-laptop    │
   │ Keep            7 daily, 4 weekly, 6 monthly               │
   ╰────────────────────────────────────────────────────────────╯
   Create this? [Y/n]: y
     Repository saved %APPDATA%\Backer\config.yaml, passphrase in DPAPI (user)
     Repository initialised, job saved. Scheduling needs Administrator:
         backer agent install --mode local

   The same setup without the wizard:
     backer repo add nas-backups --init --type smb --host nas.local \
         --share Backups --path Laptops/matt-laptop --username matt \
         --password-stdin --generate-passphrase --passphrase-out C:\keys\nas.key
     backer job create documents --repo nas-backups --exclude node_modules \
         --source C:\Users\matt\Documents --schedule "0 2 * * *"
     backer agent install --mode local
   ```

Acceptance:

- `test_every_wizard_step_has_a_flag` walks the wizard `steps` table and asserts each entry's flag
  exists in `backer init`'s click parameter list, and `test_wizard_prints_reproducible_command`
  re-parses the wizard's closing line through the click parsers and asserts it yields the parameter
  dict the wizard executed.
- `test_init_no_tty_exits_2_naming_missing_flags` invokes `main` with `["init", "--type", "smb",
  "--host", "nas.local", "--username", "svc"]`, `isatty` patched False and stdin empty; asserts exit
  code 2, that the output names `--share`, `--password-stdin` and `--path`, and that it contains no
  prompt text.
- `test_generate_passphrase_no_tty_requires_an_output` asserts `repo add r1 --init --type local
  --path DIR --generate-passphrase --headless` off a TTY exits 2 naming both `--passphrase-out` and
  `--print-passphrase`, and that the same command plus `--print-passphrase` exits 0 and prints six
  words.
- `test_every_command_in_the_plan_exists` extracts every `backer <command>` string from
  `serverless-backups.md` and asserts each resolves through the click tree with the flags listed in
  step 2, and `test_no_backend_flag_survives` asserts no click parameter anywhere under `src/` is
  named `--backend`.
- `tests/test_cli_serverless.py::test_choice_contents_are_exactly_the_v1_matrix` asserts `--type`
  renders `local|smb|s3` and nothing else. The CI cross-check
  `tests/test_workflow_sanity.py::test_cli_choices_match_ci_jobs` is Phase 7's bullet: it cannot
  pass until Phase 7 puts the serverless jobs in the mandatory result loop at
  `.gitea/workflows/release-validation.yml:336-342`, which today lists only PYTHON_CI,
  PROTOCOL_CONTRACT, S3_CONTRACT and DOCKER_BUILD.
- `tests/test_backends.py::test_kopia_backup_reports_progress_from_stderr_frames` runs a stub kopia
  that writes two recorded `\r`-separated frames to stderr and a result document to stdout, and
  asserts `progress_callback` fired at least twice with non-decreasing bytes, that `--progress` was
  in the argv, and that no callback fired for a stub emitting only the result document.
- `tests/test_backends.py::test_kopia_restore_reports_processed_of_total` feeds
  `Processed 17 (216 MB) of 60 (720 MB).` and asserts a determinate percentage reaches the callback.
- `test_snapshots_empty_reports_the_status_probe` asserts that with `list_snapshots` returning `[]`
  and the Phase 4 status probe returning "invalid repository password" the command prints that
  message and exits 1, and that `KopiaBackend.test_connection` is never called on this path.
- `printf 'aardvark-basil-cobweb-dulcet-ember-fjord' | backer repo add r1 --init --type local --path
  TMP --passphrase-stdin --headless` exits 0 (`--headless` is required off a TTY: it is the opt-in
  to the protected local-file secret fallback), and then `backer job create t1 --repo r1 --source TMP2
  --no-schedule && backer job run t1 --no-progress && backer snapshots t1 --json` exits 0 with stdin
  from `/dev/null` on Linux and from `NUL` on Windows, the last command emitting one snapshot
  object, proving every remaining prompt is reachable by flag with no TTY. The passphrase is
  supplied for real because `_connect_repo` refuses to spawn kopia without one
  (`backends/kopia.py:181-182`), and a repository created without a passphrase would prove the
  opposite of what this gate is for.
- `tests/test_cli_serverless.py::test_local_systemd_install_requires_linger` asserts `agent install
  --mode local` on Linux exits non-zero and names `loginctl enable-linger` when lingering is off.
- `test_sigint_exits_130_and_disconnects` sends SIGINT (CTRL_BREAK on Windows), asserts exit 130 and
  that `repository disconnect` ran, and `test_hard_killed_run_names_repo_unlock` asserts the message
  after a kill names `backer repo unlock` and the per-repository config file the Phase 0 fix
  isolates, because a kopia connection abandoned mid-run is a stale config entry, not a repository
  lock.

## Phase 6 - the Avalonia desktop client

Do this after Phases 1, 2, 4 and 5. The client is a view over `config.yaml` and the `backer` CLI,
never a second implementation of either (D8).

**Status, stated honestly.** The Tk implementation of this phase was built and is now deleted:
`src/backer/agent/gui/` and `tests/test_gui_serverless.py` are gone from the tree, and every step
below that used to prescribe a Tk API prescribes an Avalonia one instead. The replacement is a
rebuild in progress on this branch at `desktop/`, not shipped work: `desktop/Backer.Desktop.sln`,
`desktop/Backer.Desktop/` (Avalonia + Fluent theme + CommunityToolkit.Mvvm + YamlDotNet, version
0.9.0) and `desktop/Backer.Desktop.Tests/` (xunit, plain). Nothing in this phase may be described
as done until its acceptance bullet is green. What has landed since that sentence was written, each
verified against the tree: `agent install --mode local` passes a per-platform `--method`
(`task` on Windows, `systemd` elsewhere) instead of relying on a default the CLI rejects; Edit is a
real `backer job set` spawn; recovery-record export runs `repo passphrase NAME --passphrase-out
FILE`; Settings has a Test-scheduled-run button (`agent test-schedule`), repository remove and
export, and a service-only Remove agent service; pause durations go through `schedule pause --until`
with an ISO 8601 local-offset value; `Services/NotificationService.cs` delivers notifications under
the A5 policy; the Run view carries a stalled label; and Cancel is cooperative on Linux
(`Services/CliRunner.cs`). None of these is on a dropped or deferred list. What did carry over is
the behaviour: the view list,
the five-confirmations rule, the passphrase step, the no-percentage-without-data rule, the
notification policy and the no-engine-control guard were framework-neutral and are restated here
unchanged in substance.

Dependencies, stated rather than boasted about. The previous draft's "no new dependencies" is false
now and should not be pretended away: this phase adds a second toolchain. The client publishes
self-contained per platform (`dotnet publish -c Release -r win-x64|linux-x64 --self-contained
-p:PublishSingleFile=true`), so the installed machine needs no .NET runtime of its own and the
installer carries the cost as payload size. Against that, `pystray` and `pillow` leave
`pyproject.toml`'s win32 extras, and `tkinter` leaves the frozen build's `hiddenimports`.

1. One container, one navigation function. A single `ContentControl` in the main window swaps the
   current view's ViewModel; views are constructed lazily on first show and retained, the window
   subtitle is set from the view, and focus moves to that view's primary control. Window 760x560
   with a 700x480 minimum, every view laid out to that minimum, so only lists scroll.
   - No `TabControl` (tabs imply peers; this is a hub with flows), no second top-level app window (a
     second one is a second sizing, centring, icon and theming story), no modeless children. Modal
     confirmation dialogs are the one exception and are capped at the five in step 5. Every
     non-Home view carries Back on Escape; Enter fires the primary button.
   - One project. Views and their ViewModels are separated from the shell - the shell owns the
     window, navigation, the status strip, the tray and notifications - and every process launch
     goes through one `BackerCli` service so no view spawns `backer` on its own.

2. Ship exactly these views. Anything outside the D2 six-cell matrix has no control at all: not
   greyed out, not "coming soon", absent.
   - Welcome, shown only when the config holds neither a repository nor a server: one fork,
     serverless or join a server, with a footer stating that both can coexist (D5). Never seen again
     once either exists.
   - Home, the job list (step 3).
   - Add repository, step 1 Choose storage: a network share, a drive on this computer (which opens
     `IStorageProvider.OpenFolderPickerAsync` and jumps to step 4), or S3-compatible storage (six
     fields, then step 4). There is no engine control anywhere in this wizard - Kopia is the only
     backend, and a radio group with one member is a question with no answer.
   - Step 2 Name the file server: a hostname or UNC field with previously-used hosts listed beneath
     it, and no network scan (see Not building, item 2). Step 3 Pick the share and folder: the share
     list from `backer repo discover --json`, then the folder typed as a path under it, with the
     resolved full path echoed before Continue. Step 4 Passphrase (step 6 below).
   - **The lazily expanded folder tree and the New folder action are deliberately deferred**, and
     recorded here rather than quietly dropped alongside the other two reductions (per-frame NDJSON
     progress in Phase 5 step 6, `Avalonia.Headless` in this phase's acceptance). Neither can be
     built against the CLI that exists: `repo discover` enumerates shares on one named host and
     nothing below them, and there is no create-directory command at all, so a tree would mean the
     client walking SMB itself - a second implementation of `core/smb_browse.py` in a second
     language, which is exactly what D8 forbids. Trigger to build it: a `repo discover`-style
     directory listing on the CLI surface - one command that lists the children of a share path as
     JSON, plus a create-directory action behind the same confirmation rules - at which point the
     tree is a view over its output and the New folder action is one more spawn.
   - Step 5 Source and schedule: job name prefilled from the folder name,
     `OpenFolderPickerAsync` for the source, a collapsed exclusion list, four schedule radios
     compiling to cron with a plain-English sentence, and a disclosure for a raw expression. Cron
     validation is not reimplemented in C#: the four radios generate expressions the client knows
     are valid by construction, and a hand-typed expression is validated by the write itself -
     `job create`/`job set` rejects it with exit 2 and the CLI's own message, shown verbatim. A
     second cron parser in a second language is a drift risk for a field the user rarely touches.
   - Step 6 Review: the whole decision on one screen, each line clickable back to the step that set
     it. This is the wizard's only write, and it is one `backer repo add` followed by one `backer
     job create`; on failure nothing is left half-written, the job row is removed and the status
     strip carries the CLI's error text.
   - Run: live progress (step 8). Restore: three stacked panels in one view rather than a second
     wizard - the snapshot table, everything or one folder, and A4's three destinations. Settings:
     mode toggles (both may be on), repository and passphrase, unattended identity, appearance,
     logs, updates, a "test a scheduled run now" control, which spawns `backer agent test-schedule`
     (the command drives the hidden `agent scheduled-test TOKEN` leg through the scheduled identity)
     - the only way to reproduce the D6 failure before 2am does - repository remove and recovery-
     record export, and the "Remove agent service" control, which shells `backer agent uninstall
     --mode server --service-only --yes` and therefore cannot delete config, data or the keystore.

3. Home is the job list, not a connection form. Rows carry job, source, repository, schedule and
   last-run state; selection enables Back up now, Restore, Edit, Remove, and double-click backs up.
   Edit is `backer job set` (Phase 5 step 2); Remove is `backer job rm --yes`.
   - Rows render instantly from `config.yaml`, read directly. Last-run state and size come from
     `<data_dir>/last_attempt/` and `runs/` on a background task at view entry and every 60s after,
     so those cells read an ellipsis until it completes, never 0 and never blank.
   - The empty state replaces the list body with one sentence and one centred add button, and
     disables rather than hides the row actions so the layout does not jump when the first job
     appears.
   - Draw a server-coexistence strip only when the config holds a `server:` block, stating that
     server-managed jobs are not listed here. Never call the server from this view.

4. No colour literals; theme by variant. Avalonia's `FluentTheme` plus `ThemeVariant` already
   resolves light and dark from the OS, so the luminance-probing mechanism the Tk draft needed does
   not exist here and is not recreated. Colours come from theme resources referenced by name
   (`Body`, `Muted`, `Success`, `Danger`, `Mono`); no control carries a hex literal, in `.axaml` or
   in code-behind.
   - Every foreground/background pair meets 4.5:1 in both variants, and the pairs are asserted, not
     eyeballed.
   - Status is never colour-only: every state cell carries a word (OK, Failed, Running, Never run).
     Settings offers an explicit Light/Dark override, which sets `RequestedThemeVariant`.

5. Modal dialogs are rationed to five. A persistent status strip along the bottom of the window
   carries connected, saved, started, failed, updated. A modal confirmation exists for exactly five
   irreversible actions - restore over original files, remove job, remove repository, quit during a
   run, reveal passphrase - each behind one named confirmation method, and nowhere else. The count
   is the rule; the Tk draft's "34 down to 5" was a description of the file being replaced.
   - Each of the five maps to a CLI flag that carries the same confirmation non-interactively
     (Phase 5 step 2), which is why the client can perform them at all: `--yes-replace`,
     `--yes`, `--confirm-name NAME`. The dialog is the confirmation; the flag is how it is
     transmitted. The client never passes a confirmation flag the user did not just click.
   - Errors never live only in the strip: every error also lands in the Run view's Details pane and
     in the log file, so a line that auto-clears is never the only record. Inline errors attach to
     the control that caused them - a rejected file-server password clears and refocuses that field,
     an unwritable folder puts a line under the tree and leaves Continue disabled - rather than a
     dialog that must be dismissed before the user can act.
   - The error-1219 panel shows the CLI's A2 output verbatim, including the conflicting connection
     the CLI names from `_find_existing_connection` (`agent/service.py:222-241`). The client does
     not compose that text and does not tear down the user's own Explorer session silently.

6. Build the passphrase step to be completed rather than clicked past (D4). Generation, display and
   the position confirmation are A1, unchanged. The client never generates the words: it spawns
   `backer repo add ... --generate-passphrase --print-passphrase` and displays what comes back, so
   the EFF wordlist and `secrets.choice` stay in one place (D4). Two panels swapped inside the step
   4 view with Back disabled until it completes - not a second window (step 1). Copy uses Avalonia's
   `IClipboard`. Continue stays disabled until the position confirmation matches and the "I have
   saved this somewhere other than this computer" checkbox is ticked. Name the actual keystore
   backend in the closing paragraph from `backer keystore status --json`, and repeat that line in
   Settings when `file_fallback` is true so a downgraded store cannot become a forgotten default.
   When step 3 lands on a folder that already holds a repository, this becomes one masked field
   passed to `repo add --attach --passphrase-stdin` and verified by that command - the
   second-machine path and the shortest route through the wizard.

7. State the concurrency rule once and apply it everywhere. Anything touching the network, the
   filesystem at depth or a child process is `async`/`await` off the UI thread; results reach the UI
   through `Dispatcher.UIThread`. No UI object is touched from a background thread, and no read
   blocks the UI thread - including the startup health probe.
   - Each async surface holds a generation counter and a `CancellationToken`. Work captures the
     generation on entry and every continuation returns early when it no longer matches; Back,
     Cancel and Stop bump the counter and cancel the token, so a cancelled listing is discarded
     rather than painted.
   - Cancel is stopping the child process, and that is now the whole story. The CLI owns the kopia
     connection and its own `finally` disconnects, so there is no cross-language lifecycle
     registration to build and no repository the client has to remember to release. The stop is
     per-platform and the status text is generated from the platform rather than hard-coded:
     SIGINT plus a five-second grace then a tree kill on Linux, an immediate tree kill on Windows
     (`Services/CliRunner.cs`, `StopWording`). Quit-during-a-run
     stays one of the five confirmations, because stopping a backup mid-snapshot is still a decision
     the user should make deliberately, and the client stops the child rather than orphaning it.
   - The client holds no engine state across process boundaries. Everything it knows about a run it
     re-reads from disk, which is also what lets it show a run the SYSTEM task started.

8. Feed the Run view from the coarse progress document, and never draw a percentage that did not
   come from data.
   - The transport is Phase 5 step 6's: spawn `backer job run NAME --json`, take the `run_id` from
     the first stdout line, poll `<data_dir>/progress/{run_id}.json` at about 4 Hz and tail
     `<data_dir>/logs/{run_id}.log` for the Details pane. Poll on a timer, not per line; the
     document is whole-file replaced so a read is always a complete state.
   - What that yields is three transitions, not a frame rate, so the bar is indeterminate for the
     whole of a backup and the label says what phase it is in and how many bytes have been recorded.
     A synthetic percentage is never drawn. This is a real reduction against the Tk draft's
     per-frame bar and is recorded as such: per-frame NDJSON is deferred with its trigger condition
     in Phase 5 step 6, and when it lands this view gains a determinate bar with no other change.
   - With no update for five seconds the label says so and keeps the last figures, so a stalled
     transfer looks different from a slow one.
   - The Restore view shows the CLI's output on an empty snapshot list - which is the Phase 4 status
     probe's verdict (Phase 5 step 8) - never a blank table. After a failure the Run view gains
     copy-error and open-log-folder actions beside the Details pane.

9. Make the tray the product's face while the window is closed, and add the notifications that do
   not exist today.
   - Avalonia's `TrayIcon` works on **both** Windows and Linux, so the Tk draft's limitation - no
     tray on Linux, because `pystray` and `pillow` were win32-only extras, leaving the window as the
     only surface - is removed rather than worked around. Claim it: close-to-tray, per-job Back up
     now, and Pause backups behave identically on both platforms.
   - Create the tray icon at launch, not on the first successful agent start, so close-to-tray never
     depends on what happened earlier in the session.
   - One notification helper (`Services/NotificationService.cs`, shipped): `notify-send` on Linux
     when it is installed, and the in-window status strip everywhere else - including Windows,
     because no maintained toast library targets this Avalonia version and a second one is not worth
     a dependency for a banner. Every path falls back to the strip; the helper never fails a run.
     Trigger to add real Windows toasts: a toast library that builds against the pinned Avalonia.
   - Policy, unchanged and the important half: failures at most once per job per day (A5), the first
     success of each job once, anything needing user input, nothing else. A nightly success toast is
     how a backup tool teaches people to mute it.
   - Tray menu: Back up now per job, Pause backups (an hour, until tomorrow, until turned back on)
     with a greyed icon and a Paused header, and open-to-that-run on a failure notification click.
     Pause is `backer schedule pause [--until]`, resume is `backer schedule resume`, and the menu's
     state is read from `backer schedule show --json` rather than remembered in the client - the
     scheduler runs whether or not this process does.

10. Ship no client surface before the CLI path behind it passes an end-to-end test. Each view maps
    to a Phase 5 command and a Phase 7 matrix cell; a view whose command has no passing job on that
    platform is not built and not linked. This rule is stronger under D8 than it was: "the CLI path
    behind it" is now literally the mechanism, not merely the gate, so a view with no green command
    behind it is a view that cannot function. Adding a cell later is one radio and one list entry,
    which is the right amount of work for a cell that has just earned its test.

Acceptance:

- `desktop/Backer.Desktop.Tests::ThemeTests.NoColourLiteralsAndTokensMeetContrast` asserts by source
  scan that no `#RRGGBB` literal and no `Color="..."` appears in any `.axaml` or `.cs` under
  `desktop/Backer.Desktop/`, and that every named foreground/background token pair meets 4.5:1 in
  both `ThemeVariant.Light` and `ThemeVariant.Dark`.
- `::NavigationTests.OneContainerAndNoSecondWindow` asserts by source scan that `desktop/` declares
  no `TabControl` and constructs no second top-level app window. Modal dialogs are the sanctioned
  exception - `Views/ConfirmDialog.cs` constructs a `Window` and shows it modally, which is how step
  5's confirmations are drawn - and they are bounded by the five-confirmations rule and its own
  acceptance bullet below, not by this one. And by ViewModel test that
  navigating leaves exactly one active view and that Escape returns to Home from every non-Home
  view.
- `::DialogTests.ConfirmationsAreTheFiveIrreversibleActions` asserts at most five modal-dialog
  construction sites under `desktop/`, each inside one of the five named confirmation methods, and
  that each passes its matching non-interactive flag (`--yes-replace`, `--yes`, `--confirm-name`)
  only on a positive result.
- `::EngineTests.NoEngineControlExists` asserts the string `kopia` appears in no `.axaml` and in no
  user-visible string under `desktop/`, so the one-backend decision cannot leak back in as a
  disabled radio. This is the "one backend" guard and is worth keeping in exactly this form.
- `::PassphraseTests.ContinueRequiresConfirmationAndCheckbox` asserts Continue stays disabled until
  the A1 position confirmation matches and the checkbox is ticked, and that the ViewModel generates
  no words of its own - the passphrase reaches it only from a stubbed CLI result.
- `::ProgressTests.NoPercentageWithoutData` feeds progress documents through the ViewModel and
  asserts that a document with `total_bytes` null yields an indeterminate bar and exposes no numeric
  percentage, that byte counts render as they arrive, and that five seconds with no update sets the
  stalled label while keeping the last figures.
- `::CliTests.EveryMutationIsASpawnAndNoSecretOnArgv` asserts the ViewModel layer contains no write
  to `config.yaml`, no keystore call and no `kopia` invocation, and that every argument list the
  `BackerCli` service builds is free of a sentinel secret which is instead written to the child's
  stdin. This is D8 enforced rather than described.
- `::MessagesTests.NoFailureMessageLiterals` asserts no string literal under `desktop/` matches the
  Appendix A catalogue's failure vocabulary, so the client cannot grow a second copy of text
  `backer.core.messages` owns.
- D5 proof, restated identically to Phase 1's acceptance bullet: **the desktop client's install
  action shells `backer agent install --mode server`, which produces a scheduled task whose run
  leads to a heartbeat within 90 seconds.** Settings' unattended control is the same shell-out under
  `--mode local`. `create_background_scheduled_task` (`client/windows_service.py`) succeeds
  where `_prepare_service_config` (same file) previously raised `FileNotFoundError`. That is the
  falsifiable D5 proof, and the full form of the check -
  `schtasks /run`, the service log line, `GET /api/v1/clients/{id}` - is Phase 1's bullet, not a
  second version of it.
- `dotnet build -c Release desktop/Backer.Desktop.sln` and `dotnet test desktop/` both exit 0 on
  Linux and Windows, driven by the dedicated CI job Phase 7 step 2 adds. There is no UI-automation
  harness in v1: `Avalonia.Headless` is the intended future home for interaction tests and is
  **explicitly deferred**, so every bullet above is either a source scan or a plain ViewModel test.
  Trigger to adopt it: a regression that a ViewModel test structurally cannot catch, meaning one in
  layout, focus order or input routing.

## Phase 7 - CI and the honest support matrix

The v1 matrix from D2 is six cells: kopia to a local directory, to an SMB share and to S3, each on
Linux and on Windows. Every cell gets one named CI job in `.gitea/workflows/release-validation.yml`
that drives the serverless path end to end - non-interactive flags, keystore write, `run_backup`,
`run_restore`, sidecar read-back - and no cell is named in README, in `backer --help`, or in a CLI
`Choice()` until its job is green and mandatory. Two of the six need no new infrastructure at all:
CI is further along than the rest of this document assumes.

**Status.** Substantially on disk. `.gitea/workflows/release-validation.yml` defines
`serverless-local`, `serverless-smb-linux`, `serverless-smb-windows` and a `desktop-client` job that
runs `dotnet build`/`dotnet test` on the solution, and `SERVERLESS_LOCAL_RESULT`,
`SERVERLESS_SMB_LINUX_RESULT`, `SERVERLESS_SMB_WINDOWS_RESULT` and `DESKTOP_CLIENT_RESULT` all
appear in the mandatory result loop. `tests/test_serverless_e2e.py` is the end-to-end suite those
jobs drive, `scripts/bump_version.py` exists and all five version sites read `0.9.0`,
`backer-agent.spec` is at the repo root, and `tests/test_serverless_modes.py` pins
`desktop/Backer.Desktop/Services/Cells.cs` against `backer.serverless.cells`. Not verified here:
whether those jobs are green on a release tag, and the README rewrite in step 9.

1. S3 needs no new job and no new infrastructure, only a rename and a repair. `s3-contract`
   (`release-validation.yml:77-114`) already starts MinIO, installs the pinned kopia with `backer
   setup --quiet` (`:104-107`), and runs `tests/test_s3.py::test_s3_minio_end_to_end` (`:226-266`),
   which drives `KopiaBackend` through connect, two backups, `list_snapshots`, restore, `prune` and
   `check` against real S3. There is no credential model to build and no service container to
   design.
   - Both names are stale. `.gitea/` and `.github/` received no change in any commit since
     `ffe31b6`, so the job is still `name: Restic S3 Contract` (`:78`) and its final step is still
     `Run Restic S3 end-to-end contract` (`:109`), for a product with no restic. Rename both.
   - The job is green and asserts nothing. `test_s3_minio_end_to_end` skips unless all four
     `BACKER_TEST_S3_*` variables are set (`tests/test_s3.py:227-234`); the workflow's `env:` block
     (`:110-113`) sets three and omits `BACKER_TEST_S3_BUCKET`, so `pytest -k minio_end_to_end`
     exits 0 having skipped its only test. Add the variable in the same commit as the rename. Until
     it is added, S3 is an advertised cell with nothing behind it - the exact failure this gate
     exists to prevent - and `backend.check(repository)` at `tests/test_s3.py:266` has never once
     run against the non-existent `repository validate-client` command.
   - Windows is a second leg on the same job, not a second job. Keep the id `s3-contract` so
     `needs:` (`:318`) and `S3_CONTRACT_RESULT` (`:330`) keep working, add `strategy.matrix.os:
     [ubuntu-latest, windows-latest]`, and set `name: Kopia S3 Contract (${{ matrix.os }})`. The
     ubuntu leg keeps the container pinned at `:94-97`. The windows leg cannot use it: the hosted
     `windows-latest` Docker daemon runs Windows containers and `minio/minio` is a Linux image.
     Download the pinned MinIO Windows binary and run it as a background process, verified against
     the publisher's checksum manifest the way `TOOL_INFO` pins kopia 0.23.1
     (`tools/manager.py:20-38`, checksum URL at `:24`, fail-closed rule at `:17-19`), and keep the
     same health poll.
2. Add both local-directory legs as one matrix job `serverless-local`, `strategy.matrix.os:
   [ubuntu-latest, windows-latest]`, copying the `protocol-contract` shape at
   `release-validation.yml:49-75`: checkout with `ref: ${{ inputs.release_tag || github.ref }}`,
   `setup-python` 3.11, `pip install -e ".[dev]"`, `backer setup --quiet`, then `python -m pytest -q
   tests/test_serverless_e2e.py -k local`. This job is Python only: the `xvfb-run -a python -m
   pytest -q tests/test_gui_serverless.py` second step an earlier draft put on the `ubuntu-latest`
   leg is deleted along with the Tk suite it ran, and no `xvfb` or display server is needed by
   anything in the Python tree any more.
   - The desktop client gets its own job, `desktop-client`, rather than a step inside this one.
     Mixing `actions/setup-dotnet` into a pytest job to run one command buys nothing and makes a
     Python failure and a C# failure share one red X. `strategy.matrix.os: [ubuntu-latest,
     windows-latest]`, `actions/setup-dotnet` at the pinned SDK the csproj targets, then `dotnet
     build -c Release desktop/Backer.Desktop.sln` and `dotnet test desktop/`. It uploads no
     artifacts, for the reason step 7 gives.
   - `desktop-client` is mandatory, not optional, and that follows from this phase's own rule rather
     than from taste: an optional leg cannot support an advertised cell, and the client advertises
     repository types. It joins `needs:` (`:315-321`), the `env:` block (`:328-333`) as
     `DESKTOP_CLIENT_RESULT`, and the mandatory loop (`:336-342`) alongside the three serverless
     names in step 5.
   - The repository is a directory under the runner's temp path, so this leg is also D3's "Linux
     unprivileged, the user pre-mounts and it is a `local` repository" branch by construction and
     needs no job of its own.
   - Neither runner has a Secret Service session, so this is where the headless keystore fallback is
     exercised.
   - Both legs assert `KOPIA_CONFIG_PATH` isolation directly: two jobs against two repositories, run
     concurrently, both succeed. Without the Phase 0 fix each one's `finally: _disconnect_repo()`
     (`backends/kopia.py:383-384`) tears down the other, so this is the cheapest cell that proves
     the fix.
3. Add `serverless-smb-linux` on `ubuntu-latest`, cloned from the `s3-contract` job at
   `release-validation.yml:77-114`. Copy its four properties exactly:
   - one step that pins an image and runs it detached, the way `:94-97` pins
     `minio/minio:RELEASE.2025-09-07T16-13-09Z`; pin the Samba image to an exact tag or digest,
     never a floating `latest`;
   - a health poll inside that same step - the `for attempt in $(seq 1 30) ... sleep 1; done; exit
     1` loop at `:98-102` - probing port 445 instead of an HTTP endpoint, so a slow container start
     fails the step rather than the test;
   - credentials supplied as `env:` on the pytest step only, as at `:110-113`, never inline in a
     shell line - and every variable the test reads, which is the mistake step 1 repairs; and
   - a test that self-skips when its variables are absent, the way `test_s3_minio_end_to_end` skips
     at `tests/test_s3.py:227-234`, so a plain `python -m pytest -q` stays green on a developer
     machine with no Samba server.
   - `sudo apt-get install -y cifs-utils` first, then let the test drive the real `mount -t cifs`
     path at `client/agent.py:608` inside `_smb_mount_context` (`:580`). The share password goes
     through the auth file; the workflow contains no `password=` on any command line. Kopia never
     sees SMB: `_get_repo_type` returns `("filesystem", ["--path", path])`
     (`backends/kopia.py:129-133`), so what this leg proves is the mount, not a kopia provider.
4. Add `serverless-smb-windows` on `windows-latest`, with no container. A Samba container started on
   an `ubuntu-latest` runner is a different machine with no route from a Windows runner, so the
   Windows runner is its own SMB server:
   - `New-Item -ItemType Directory C:\share`, then `New-SmbShare -Name Backups -Path C:\share
     -FullAccess Everyone`;
   - `New-LocalUser` a dedicated account and grant it access, so the test authenticates with
     explicit credentials rather than falling through on the runner's own token - the only way
     `_connect_with_explicit_credentials` (`agent/service.py:277-327`) actually executes;
   - back up to `\\localhost\Backups`, which exercises the real Windows redirector, `net use` and
     `SMBConnectionManager` (`agent/service.py:97-394`) rather than a mock; and
   - one case that connects to the same server twice under different credentials, so D6's error-1219
     story (`agent/service.py:183-194`, the branch at `:184`) has a regression test instead of a
     paragraph.
5. Windows CI is mandatory, and this settles it. The only Windows leg outside `protocol-contract`
   today is `windows-agent-package`, gated `if: inputs.build_windows_agent == 'true'` at
   `release-validation.yml:285` against a `workflow_dispatch` default of `"false"` at `:19`, and
   accepted as `skipped` by the permissive loop at `:343-349`. Under the standing rule an optional
   leg cannot support an advertised cell, so `serverless-local` and both SMB jobs go into `needs:`
   (`:315-321`), into the `env:` block (`:328-333`) as `SERVERLESS_LOCAL_RESULT`,
   `SERVERLESS_SMB_LINUX_RESULT` and `SERVERLESS_SMB_WINDOWS_RESULT`, and into the mandatory loop at
   `:336-342`, which today holds only `PYTHON_CI_RESULT`, `PROTOCOL_CONTRACT_RESULT`,
   `S3_CONTRACT_RESULT` and `DOCKER_BUILD_RESULT`. `S3_CONTRACT_RESULT` is already there and now
   covers two legs instead of one. `DESKTOP_CLIENT_RESULT` joins them for the reason step 2 gives.
   None of the four new names joins the permissive loop, which accepts `skipped`. Add
   `VERIFY_VERSION_RESULT` while there: `verify-version` sits in `needs:` at `:315` and is checked
   by neither loop. If this forge has no Windows runner, the three Windows cells come out of README,
   `--help` and the `Choice()` in the same commit - there is no third option where a cell stays
   advertised and its leg stays optional.
   - **Open task, named rather than dropped: the cells-advertisement gate is now CLI-only.**
     `test_cli_choices_match_ci_jobs` (step 9's acceptance) parses the Python `click.Choice` and
     cannot see the desktop client, so a repository type could be offered in the client's storage
     picker with no green job behind it and nothing would fail. The fix is a second assertion in the
     same test, scanning `desktop/` for the type list and requiring it to equal
     `backer.serverless.cells.supported_repository_types`; the client should read that list from
     `backer repo list`-adjacent output or hold it in exactly one place a scan can find. Until that
     assertion exists the gate is half-enforced, and this bullet is the record of it.
6. Fix the version bump before editing any workflow. The version is duplicated across
   `pyproject.toml:7`, `src/backer/_version.py:3`, `installer/backer-agent.iss:13`,
   `android/app/build.gradle.kts:19` and now a fifth file, `desktop/Backer.Desktop/`'s csproj
   `<Version>`, with the derived Android `versionCode` at `:18` computed as
   `major * 10000 + minor * 100 + patch`, while `make release` seds only `pyproject.toml`
   (`Makefile:92`). A release cut by the documented command therefore fails `verify-version` and
   `tests/test_workflow_sanity.py::test_release_version_files_match` (`:32-49`).
   - Add `scripts/bump_version.py`, writing all five files and computing `versionCode` with the same
     arithmetic the test uses at `test_workflow_sanity.py:25-29`; make the `release` target at
     `Makefile:87-98` call it instead of the inline `sed`. Extend
     `test_release_version_files_match` to the csproj in the same commit - a version file the test
     does not read is a version file that drifts, which is the defect this step exists to fix.
   - It refuses to run when `CHANGELOG.md` has no `## <new version>` section, so the bump and the
     notes cannot diverge.
7. Treat workflow YAML as tested source. `tests/test_workflow_sanity.py` hard-pins the exact ordered
   artifact-action lists of `.github/workflows/release.yml` (`:222-233`) and
   `.github/workflows/gitea-release.yml` (`:278-289`), the `with:` dict of every upload and download
   (`:240-244`, `:300-304`, `:320`), and the literal `if:` guard strings (`:216`, `:250-256`,
   `:272`, `:312-315`). Any edit to those two workflows lands in the same commit as its test edit.
   The new jobs go in `.gitea/workflows/release-validation.yml`, which those ordered lists never
   read, and they upload no artifacts - keep it that way, because one `actions/upload-artifact` step
   added to a release workflow shifts a pinned list and breaks two tests. Exactly one test reads
   `release-validation.yml` at all - `tests/test_workflow_sanity.py:442-449` - and it pins the
   MinIO image line at `:449` and no job name, so the rename in step 1 breaks nothing and is also
   pinned by nothing. Extend that test in the same commit with both renamed S3 strings, the pinned
   Samba image string and the pinned MinIO Windows binary version, so the misnomer cannot come back.
8. Write the release notes to the machine-checked format. `scripts/check_changelog.py` runs on every
   push and pull request to `main` and `dev` from both `.gitea/workflows/changelog.yml` and
   `.github/workflows/changelog.yml`, and `test_changelog_follows_the_documented_format`
   (`:452-457`) runs the same checker over the real file. Only `## <x.y.z>` headings are recognised
   (`check_changelog.py:27`); only `### Major Features`, `### Minor Features` and `### Bug Fixes`
   are permitted, in that order, each with at least one bullet (`:26`, `:61-75`); the newest section
   must equal the pyproject version (`:82-85`); no version may repeat (`:86-88`). Serverless mode is
   a Major Feature; every defect fixed in Phase 0 is a Bug Fix bullet; and the recovery procedure
   for repositories orphaned by 0.8.0 is a Major Feature bullet, because it changes how Backer is
   operated.
9. Rewrite the README to the shipped matrix.
   - Replace the storage claim at `README.md:133` with the six-cell v1 table plus a "server-managed
     mode only" column for everything outside it, in the style of `protocolfixes.md:12-17`. NFS is
     in that column: it has an agent mount path (`client/agent.py:671`) and no CI leg anywhere.
   - Change the Mobile entry at `README.md:55` to state that the Android agent is server-relay-only
     and has no serverless mode in v1 (D1).
   - Split the Local Directory section at `README.md:153-163` into two headings: `Local directory
     (server-managed, via the proxy relay)`, a directory on the Backer server reached over
     `proxy://` (`server/app.py:617-629`), and `Local directory (serverless, on this client)`, a
     directory kopia writes to directly. One paragraph cannot serve both, and `:163` currently
     describes only the first.
   - Say plainly what two machines writing one repository get: kopia's own concurrent-writer model,
     one designated maintenance owner per repository, and no cross-machine lease from Backer.
   - Rewrite the Windows install section for one installer carrying two payloads, and state the
     unsigned warning once for both: neither `backer.exe` nor `backer-desktop.exe` is code-signed,
     so SmartScreen will warn, and that stays true until the signing step in step 10 exists.

10. Repackage Windows, which this plan previously never mentioned and which the rebuild forces.
    - `backer-agent.spec` (at the repo root, which is where `Makefile`, `scripts/build_agent.py` and
      `release-validation.yml` all invoke it from) is **rewritten in place**, keeping its filename, and stops
      building a GUI. Its target is a console CLI, `backer.exe`, entry point the `backer` CLI main,
      `console=True`. `tkinter`, `tkinter.ttk` and `tkinter.messagebox` leave `hiddenimports`; the
      EFF wordlist stays in `datas`, or A1 item 1 raises `FileNotFoundError` on the first frozen
      run; `backer.serverless.scheduled_test` stays importable, because the hidden `backer agent
      scheduled-test` leg is the only way a frozen install runs D6's self-test with no interpreter
      present. `backer-agent-service.exe` is unchanged.
    - The Inno installer built from `installer/backer-agent.iss` ships three binaries where it
      shipped two: `backer.exe`, `backer-agent-service.exe` and the self-contained
      `backer-desktop.exe`. `OutputBaseFilename` stays `backer-agent-setup` - the update path
      downloads that exact name and an installed client must keep finding it. Add
      `CloseApplicationsFilter=backer-desktop.exe,backer-agent-service.exe,backer.exe`, or an
      in-place update over a running client fails on a locked file.
    - Payload size grows by the self-contained .NET publish. That is the price of D8 and is recorded
      here rather than discovered at release; no framework-dependent variant ships, because "install
      .NET first" is a support burden a backup tool does not need.
    - Update-check ownership moves to the desktop client. It checks the same release-main
      `backer-agent-setup.exe` URL the deleted GUI used, downloads it, and runs it with
      `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART` - Inno's flags, not NSIS's `/S`, which is the
      defect in the risk table below and was already fixed before the port. `tests/test_agent_gui_release_urls.py`
      is deleted with the Python GUI; the URL and the flag list are asserted in the client's own
      test project instead, so the same two mistakes stay covered.
    - Signing is still absent and is still the largest packaging risk. It now covers two unsigned
      binaries plus the installer rather than one, which makes it more urgent, not less.

Acceptance:

- The YAML defines `serverless-local` and `s3-contract` each with matrix `os: [ubuntu-latest,
  windows-latest]`, plus `serverless-smb-linux` and `serverless-smb-windows`, and all six resulting
  checks are green on the release tag.
- `grep -n "BACKER_TEST_S3_BUCKET" .gitea/workflows/release-validation.yml` matches, and the
  `s3-contract` run log reports `1 passed` rather than `1 skipped`, on both legs.
- `grep -n "Restic" .gitea/workflows/release-validation.yml` returns nothing.
- `SERVERLESS_LOCAL_RESULT`, `SERVERLESS_SMB_LINUX_RESULT`, `SERVERLESS_SMB_WINDOWS_RESULT`,
  `DESKTOP_CLIENT_RESULT` and `VERIFY_VERSION_RESULT` appear in the mandatory loop at
  `release-validation.yml:336-342` and none appears in the optional loop at `:343-349`; NEW
  `tests/test_workflow_sanity.py::test_every_needed_job_is_checked` asserts every job in
  `release-artifacts-ready`'s `needs:` (`:315-321`) is checked by one of the two loops.
- `python -m pytest -q tests/test_serverless_e2e.py` passes on a machine with no SMB share, no Samba
  container and no MinIO, skipping those cases by environment variable.
- `serverless-smb-windows` fails if `SMBConnectionManager` is stubbed out: it asserts a real `net
  use` to `\\localhost\Backups`, then a second connection under a different local account returning
  error 1219, then that the SYSTEM branch issues `net use <existing> /delete` and reconnects while
  the interactive branch exits non-zero without deleting.
- `desktop-client (ubuntu-latest)` and `desktop-client (windows-latest)` are both green: `dotnet
  build -c Release desktop/Backer.Desktop.sln` and `dotnet test desktop/` exit 0 on each, and the
  job appears in `needs:` and in the mandatory loop, never the permissive one.
- `grep -rn "tkinter\|pystray\|xvfb" .gitea/workflows/ .github/workflows/ installer/ pyproject.toml`
  returns nothing, and `grep -n "backer-desktop.exe" installer/backer-agent.iss` matches both the
  file entry and `CloseApplicationsFilter`.
- All five version sites read `0.9.0` today (`pyproject.toml`, `src/backer/_version.py`,
  `installer/backer-agent.iss`'s `MyAppVersion`, `android/app/build.gradle.kts` with
  `versionCode = 900`, and `desktop/Backer.Desktop/Backer.Desktop.csproj`'s `<Version>`), so the
  gate is stated against that baseline - bumping to 0.9.0 would produce an empty diff and prove
  nothing. With a `## 0.9.1` section added to `CHANGELOG.md`, `python scripts/bump_version.py 0.9.1`
  exits 0, `git diff --name-only` lists all five of those files, `build.gradle.kts` reads
  `versionCode = 901`, the csproj reads `<Version>0.9.1</Version>`, and
  `python -m pytest -q tests/test_workflow_sanity.py::test_release_version_files_match
  tests/test_workflow_sanity.py::test_changelog_follows_the_documented_format` passes; `python
  scripts/bump_version.py 0.9.2` against a CHANGELOG with no `## 0.9.2` section exits non-zero
  naming it and leaves all five files unchanged.
- `python scripts/check_changelog.py` exits 0, and the full `python -m pytest -q
  tests/test_workflow_sanity.py` passes in the same commit as every workflow edit.
- `grep -n "server-relay-only" README.md` matches the Mobile entry, and NEW
  `tests/test_docs_matrix.py::test_readme_names_both_local_types` asserts README contains both
  `Local directory (serverless, on this client)` and `Local directory (server-managed, via the proxy
  relay)`.
- NEW `tests/test_workflow_sanity.py::test_cli_choices_match_ci_jobs` parses the `--type`
  `click.Choice` contents against the job names in the mandatory loop and fails on any advertised
  (platform, repository type) pair with no job behind it.

## Appendix A - safety and failure UX

Every user-facing string lives here and in one module, `backer.core.messages`, created by Phase 5
step 8, so one test can lint the catalogue. There is exactly one copy and no export: the desktop
client shows the CLI's own output verbatim (D8), so the no-drift guarantee holds by construction
rather than by keeping two catalogues honest. The linting tests below keep covering Python, and the
only rule the client carries is a negative one - no failure-message literal may appear under
`desktop/` - which is a scan, not a synchronisation mechanism.
Phases 4-6 reference these subsections and never restate the strings. Commands quoted here are
spelled exactly as in the Phase 5 tree.

Copy rules for every string below:

1. Name which of the three secrets is meant, every time one is mentioned: the Windows sign-in, the
   file-server sign-in, or the repository passphrase. Users confuse them constantly and typed a
   different one seconds earlier.
2. State the machine's constraint, then the ways out, with the non-destructive one first. Never
   attribute the failure to the user.
3. No exclamation marks, no "Oops", no "Something went wrong". The two known offenders both ended in
   `now running!` and died with the Tk GUI; the rule outlives them and is enforced by a scan, so it
   applies to every string written since.
4. Report what was checked and name what was not. A green result that implies more than it proved is
   the failure mode of a backup product.
5. Every prompt and confirmation here has a flag (D7). Nothing is reachable only by answering a
   question.
6. Never name the engine to explain a Backer decision. Kopia is named only when quoting its own
   output back to the user, and then it is labelled as a quote.

### A1 - The passphrase moment

1. Generate six words with `secrets.choice` over a bundled EFF short wordlist (~8KB `.txt`) loaded
   through `importlib.resources`; add it to `[tool.setuptools.package-data]` and to the PyInstaller
   spec's `datas`, or the first frozen build raises `FileNotFoundError` here. This is the only copy
   of the wordlist that ships: the desktop client shells `--generate-passphrase` rather than
   carrying a second one (D4), because a security-relevant wordlist duplicated across two build
   systems is a wordlist that will eventually differ. Six words survive
   being written on paper and make the position confirmation below possible; `token_urlsafe` does
   neither.
2. Display all six numbered left to right, consequence first: "Backer encrypts everything it writes
   to this repository. The passphrase below is the only way to read it back. It is kept on this
   computer and nowhere else. If you lose it and lose this computer, the backups cannot be read by
   anyone, including you." Then the words, then `c` copy, `s` save to a file, `p` print, `o` use my
   own, Enter to continue. Copy prints one line, because the clipboard is not storage: "Copied.
   Paste it into your password manager now; the clipboard is not a safe place to leave it." Offer
   `c` only when `clip`, `wl-copy` or `xclip` exists, and never fail on its absence.
   "Kept on this computer and nowhere else" is only true because the Phase 4 engine passes
   `--no-persist-credentials` on every kopia invocation. `--persist-credentials` defaults on in
   kopia 0.23.1, and on Windows that writes the passphrase into Credential Manager as a side effect
   of connecting, which would make this sentence false and put a second copy somewhere `backer repo
   rm` does not clean up.
3. The recovery record is offered, never written by default. `s` (`--passphrase-out FILE`) writes a
   plain-text file holding the passphrase, the repository name, its path and the date, defaulting to
   a removable drive when one is mounted, and says so plainly: "This writes your passphrase to
   <path> in plain text." Afterwards: "A copy saved on this computer will not help if this computer
   is the one that fails. Save it somewhere else as well."
4. Clear the screen, then confirm by position so the answer cannot be read off the scrollback: "Read
   these from where you saved it. The passphrase is no longer on screen on purpose. Type word 3 and
   word 6, separated by a space:". On a mismatch: "That does not match. Words are numbered left to
   right starting at 1." Offer show-again and cancel. A third choice skips the confirmation only
   when the words are already off this computer: it is available after `s` (`--passphrase-out
   FILE`) or `p`, and otherwise requires the sentence `I have saved it` typed in full. Never lock a
   user out of their own repository over a typo, and never create a repository whose passphrase
   exists only in this machine's keystore.
   `tests/test_cli_serverless.py::test_skip_confirmation_requires_a_saved_copy` asserts the third
   choice is refused when neither save option was used.
5. A second machine joins with `backer repo add NAME --attach`: file-server sign-in, then
   passphrase, then a summary of what is already there before anything is written. Gate it on the
   explicit `repository status --json` probe Phase 4 adds, which returns kopia's `uniqueIDHex` for a
   real repository and distinguishes three failures by kopia's own words, verified against 0.23.1:
   `cannot access storage path` (the path is not there), `repository not initialized in the provided
   storage` (the path is there and empty), `invalid repository password` (there is a repository and
   this is the wrong passphrase). Never gate it on `KopiaBackend.test_connection`, which returns
   `(True, "...will be initialized on first backup")` for the middle case
   (`backends/kopia.py:226-227`) - it reports success for exactly the mistyped-path case this step
   exists to catch. On success: "Found a repository at that path. It was created on 11 Mar 2024 and
   holds 92 snapshots. This computer will add its own backups alongside them. Nothing already there
   is changed or removed." On the empty case: "There is no repository at that path. Backer will not
   start a new one here by accident. To create one, run: backer repo add NAME --init". Auto-init on
   any connect failure is already gone (Phase 0); this refusal is the second line of defence against
   a typo in a share path producing an empty repository that reports success forever.
6. A rejected passphrase, at any of those points: "That passphrase did not open the repository. The
   backup engine reports: invalid repository password. This is the six-word passphrase shown when
   the repository was created, not your Windows sign-in and not the file-server sign-in." Show an
   attempt counter, never a lockout.
7. `backer repo passphrase NAME` reprints the numbered words and the save options. When the keystore
   holds nothing, lead with what is recoverable: "Backer does not have the passphrase for Home NAS
   on this computer. It was shown once, when the repository was created. It is not stored on the
   file server and Backer cannot recalculate it. If you saved it, enter it on any computer with:
   backer repo add NAME --attach. If it is genuinely gone, the backups already in this repository
   cannot be read. The files are still on \\nas.local\backups and take up space, but nothing can
   decrypt them."
8. `backer repo rm NAME` deletes this computer's keystore entries as well as the record, and the
   passphrase is the only thing that can read the data it leaves behind. Lead with that: "The
   backups in \\nas.local\backups stay where they are. Backer will delete the passphrase for them
   from this computer. If you have not saved it somewhere else, nothing will ever be able to read
   those backups again." Offer to re-display and save it first, per item 3, then require the
   repository name typed back rather than a y/n. `--yes` does not bypass that warning or that
   prompt, and off a TTY the command exits 2 unless `--passphrase-out FILE` is given in the same
   invocation.

### A2 - File-server sign-in and the Windows error-1219 collision

1. Label the credential prompt so it cannot be mistaken for the other two secrets: "Sign in to
   nas.local. This is the sign-in for the file server, not your Windows sign-in and not the
   repository passphrase." Then Username, Password, "Domain (leave blank for none)".
2. Detect the collision on the branch that already exists, `'1219' in result.stderr` at
   `agent/service.py:184`, which today only logs, and name the conflicting connection from
   `_find_existing_connection` (`:222-241`), parsing the remote name out of the `net use` line
   rather than printing it raw. Present three choices, the non-destructive one first and defaulted:

   ```
   Windows will not open a second connection to nas.local

   Windows allows one sign-in per file server at a time, and this computer is
   already signed in to nas.local as MATT:   \\nas.local\media

   Backer is trying to sign in as backup-svc. Both cannot be open at once.

     1   Use the sign-in that is already open (MATT)
         Backer will test whether MATT can write to the backup share.
         Nothing you have open is disturbed.
     2   Close \\nas.local\media and sign in as backup-svc
         \\nas.local\media will disconnect until you open it again.
     3   Cancel

   Choice [1]:
   ```

3. Option 1 connects with no credentials, then writes and deletes a zero-byte probe file in the
   repository path; on success it stops asking and prints "Backups that run in the background use
   their own sign-in and are not affected by what you have open in File Explorer." Option 2 asks a
   second confirmation naming the share being disconnected. Cancel saves the job marked "needs
   sign-in" rather than discarding it.
4. Under the SYSTEM task there is no interactive session, so option 1 does not exist: "Documents and
   Photos both use nas.local with different sign-ins. Windows allows one sign-in per file server, so
   both jobs must use the same one. Neither job ran." Record it per A5 and show it on Home. The
   serverless path reaches a share only through `_connect_with_explicit_credentials`
   (`agent/service.py:277-327`) and never through `cmdkey`, which writes into the interactive user's
   Credential Manager (D6); `cmdkey` stays in the module for server-managed mode.
5. Permission denied, split by which half failed, because the remedies differ: "nas.local did not
   accept the file-server sign-in for backup-svc. The password may have changed, or the account may
   be locked." versus "backup-svc can read \\nas.local\backups but cannot write to it. Backer needs
   to create files in the backup folder." For a local repository under the SYSTEM task: "Backer
   cannot write to D:\Backups. In the background it runs as SYSTEM, which may not have access to
   that folder."
6. Linux, unprivileged with no gvfs (D3): the backup engine has no SMB provider, so a share must be
   a mounted filesystem path (`client/agent.py:818-831`), and with neither an existing mount nor
   `gio` there is no way to get one without root. "Backer can connect to a file server as you,
   without a password prompt, once gvfs is installed - `sudo apt install gvfs gvfs-backends` or
   `sudo pacman -S gvfs gvfs-smb`. Otherwise mount the share yourself and point Backer at the
   mounted folder as a local repository, or use S3-compatible storage, which needs no mount and no
   administrator." With gvfs present this message does not appear: a wrong password reports as a
   sign-in failure instead.

### A3 - The first prune

1. `backer prune JOB` always previews, and the preview comes from the engine's own policy evaluation
   rather than a second retention implementation. Retention is per source, not per tag: the policy
   is written against this job's source path, `policy set <user@host:path> --keep-latest ...`, so
   another computer's snapshots of the same folder are a different source and are never in scope.
   The preview is two reads, because `snapshot expire` has no `--dry-run` flag - verified against
   0.23.1, its only non-global flags are `--all` and `--delete`:
   - The count comes from `snapshot expire <source-path>` with `--delete` omitted, which is a real
     preview: it exits 0 and prints `N snapshot(s) of matt@host:<path> would be deleted. Pass
     --delete to do it.`
   - The dates for `--list` come from `snapshot list --json <source-path>`, reading each snapshot's
     `retentionReason`. A snapshot the policy keeps carries reasons such as
     `["latest-1","daily-1"]`; a snapshot the policy expires has none. Verified: after `policy set
     <path> --keep-latest 1`, exactly the two snapshots that `snapshot expire` counts come back with
     no `retentionReason`.
   Say once, on the screen, that the preview writes the retention policy before it reads it, because
   that is a stored per-source object and it stays written if the user stops here. Nothing is
   deleted by writing it, and `--apply` does not write it again.

   ```
   Repository   Home NAS   \\nas.local\backups\backer
   Job          Documents  C:\Users\matt\Documents on DESKTOP-M only
   Policy       keep the last 7 daily, 4 weekly, 6 monthly

     Snapshots now         92
     Would be kept         17    newest 2026-08-27, oldest 2025-09-01
     Would be removed      75    newest 2026-06-30, oldest 2024-03-11

   Removing a snapshot removes the ability to restore your files as they were
   on that date. Files that also exist in a snapshot being kept are not
   affected. This cannot be undone.

   Nothing has been deleted. To see the 75 dates: backer prune Documents --list

   Type the number of snapshots to remove (75), or press Enter to stop:
   ```

   Space freed is deliberately absent: it is not known until the expire and the maintenance pass
   have both run, and a number invented here is one the user will hold against the summary. Every
   count on that screen is this job's source on this computer, because that is what the source path
   means; say so on the screen, because a user who sees 92 where the repository holds 300 will
   otherwise assume the preview is wrong.
2. The confirmation is that count typed back, not a y/n. Anything else, "y" included, stops with
   "Stopped. Nothing was deleted." The scripted form is `--apply --yes-remove 75`, which re-runs the
   preview and proceeds only if it still reports exactly 75; otherwise it exits non-zero with "The
   policy now removes 81 snapshots, not 75. Nothing was deleted."
3. `--apply` is the only path that passes `--delete`, and it runs `snapshot expire <source-path>
   --delete` for this job's source only - never `--all`, which is what the code does today
   (`backends/kopia.py:696`) and which would expire every source in the repository, including other
   machines'. Follow it with `snapshot verify --verify-files-percent=1` under its own timeout,
   because expire and the maintenance pass behind it are the only operations that remove content.
   The summary names the same numbers: "Snapshots removed 75 / Snapshots remaining 17 / Space freed
   41.2 GB / Oldest snapshot now 2025-09-01 / Repository check passed", or "Repository check not
   completed (ran out of time after 62m) - run: backer verify Documents --timeout 14400". Never a
   blank, and a timeout is never reported as damage. Close with the log path from A5.
4. Nothing prunes automatically in v1, so there is no scheduled-cleanup copy to write and no runaway
   guard to word.

### A4 - Restore safety

1. Offer three destinations, the safe one first and defaulted, with the dangerous one named plainly
   rather than hidden. Header: "Snapshot 4f8a2c1, 27 Aug 2026 02:14, 41,882 files, 38.1 GB, from
   C:\Users\matt\Documents on DESKTOP-M."
   - "1  A new folder - C:\Users\matt\Documents (restored 2026-08-28). Nothing you have now is
     changed. You compare the two and move across what you need." (`--into NEW`)
   - "2  Back to C:\Users\matt\Documents, adding to what is there. Files from the snapshot are
     written over files with the same name. Anything in that folder that is not in the snapshot is
     left where it is." (`--into MERGE`)
   - "3  Back to C:\Users\matt\Documents, exactly as it was. Everything now in that folder is set
     aside first. Files you created or changed since 27 Aug 02:14 will not be there." (`--into
     REPLACE`)
2. REPLACE only: show what is about to be moved, then require the typed word.
   "C:\Users\matt\Documents currently holds 42,104 files. All of them will be moved to
   C:\Users\matt\Documents.replaced-2026-08-28-144703 and the snapshot restored in their place. You
   can delete that folder once you are happy with the restore. Type REPLACE to continue, or press
   Enter to stop:". The pre-flight behind that count cannot be a dry run - `KopiaBackend.restore`
   rejects `dry_run=True` outright (`backends/kopia.py:464-472`) - so it is the Phase 0 rebuild of
   the clean-restore validation, and this copy may not ship before that lands. Do not cite the
   twelve clean-restore tests at `tests/test_agent_protocol.py:198-546` as cover for it: four of
   them now assert the destination is deleted and the run reported green in cases their names say
   are refusals, which is the behaviour Phase 0 changes and those tests change with it.
3. The moved-aside copy is kept, not deleted: the `shutil.rmtree` at `client/agent.py:1398` becomes
   a rename to `<name>.replaced-<timestamp>` at second resolution beside the destination (the
   staging directory is already created with `dir=destination.parent` at `:1323-1325`), suffixed
   `-2`, `-3` if that name exists. `rmtree` survives only behind `--clean-up-replaced`, and a
   failure of this last step is a warning on a successful restore, never a failure of it. Closing
   copy: "What was in that folder has been moved to
   C:\Users\matt\Documents.replaced-2026-08-28-144703. Check the restore, then delete that folder
   when you are happy with it."
4. Widen the destination guard, which today refuses only a filesystem root
   (`client/agent.py:1307-1308`) and today runs only on the clean-restore branch, into a deny list
   checked for all three `--into` modes before anything is written or moved: the user profile root,
   `%WINDIR%`, `%ProgramFiles%`, `/`, `/home`, `/usr`, and any path that is or contains a configured
   repository.
   Name the rule that matched: "Backer will not restore over C:\Users\matt. Choose a folder inside
   it instead."
5. Restoring onto a machine that has never seen the repository is one command, with no prior
   configuration and no server: `backer restore --from //nas.local/backups/backer`. Ask the
   file-server sign-in, then the passphrase, both labelled per A2 and A1, then list snapshots. The
   listing must name the computer each snapshot came from, and today it cannot: `list_snapshots`
   reads `hostname` and `username` from the top level of kopia's JSON (`backends/kopia.py:610-611`),
   where they do not exist - kopia puts them at `source.host` and `source.userName`, so both fields
   are always `None` and `--computer NAME` has nothing to filter on. Phase 4 reads them from
   `source`. Never consult the keystore; this machine's is empty by definition. Show the source path
   in full
   and never match on its last component: `_find_latest_snapshot_for_source` falls back to a
   basename match (`backends/kopia.py:425-428`), which makes `C:\Users\a\Documents` and
   `D:\Archive\Documents` the same snapshot, and Phase 0 removes that fallback. Check free space
   first and state both numbers plus what is not at risk: "D:\recovered\Photos does not exist and
   will be created. D: has 1.8 TB free. This restore needs about 412.7 GB. Nothing already on this
   computer is changed." Close with the two commands that turn a recovery machine into a backed-up
   one: `backer repo add NAME --attach`, then `backer job create`.

### A5 - Unattended failure

There is no notification path and no local run log today, so a 2am SYSTEM-task failure reaches
nobody. Build three layers, because a toast at 2am is also seen by nobody.

1. Every run writes a local record through `backer.serverless.store.append_run`, success or failure,
   including pre-flight failures - a missing passphrase, an unreachable share, a rejected
   file-server sign-in, a locked keystore - so none of them can be silent for weeks. The repository
   sidecar stays the cross-machine copy. This relies on the Phase 0 fix to the `if result.success:`
   gate.
2. Notify, at most once per job per day: "Backer / Documents backup did not run / Could not reach
   \\nas.local\backups. Open Backer for details." A failure with no client running sets a pending
   flag on the run record, shown when the client next starts - which is clean either way, because
   the flag is on disk and the writer is the CLI.
3. Change persistent state as well as toasting. The tray tooltip becomes "Backer - 1 backup needs
   attention", on both platforms now that the tray exists on Linux too (Phase 6 step 9), and Home
   and `backer status` read entirely from the local records, so both render
   fully with the share offline: "Documents / Home NAS / last successful backup 2 days ago (26 Aug
   02:14) / not working", then "Documents: 3 failed attempts, most recently today at 02:14. Could
   not reach \\nas.local\backups. backer status Documents --why".
4. Fix the log path, or "check the log file" is not an instruction anyone can follow.
   `setup_agent_logging` resolves `%APPDATA%/Backer/logs` (`agent/service.py:55-59`), which under
   the SYSTEM task is `C:\Windows\System32\config\systemprofile\AppData\Roaming\Backer\logs`, while
   an Open logs button points at the interactive user's directory. One resolved path is the whole
   point: the client opens whatever `get_data_dir()`-derived directory the CLI reports, never a path
   of its own. When
   frozen or running as a service, log under `%ProgramData%\Backer\logs` and grant Users read on
   that subfolder, because the `icacls` call at `client/windows_service.py:61-62` strips Users
   entirely. On Linux use `$XDG_STATE_HOME/backer/logs`, else `~/.local/state/backer/logs`, and
   `/var/log/backer` only as root under a system unit.
5. `backer status JOB --why` turns an exit code into one sentence plus the raw output, and never
   invents a cause: "Backer could not reach \\nas.local\backups at 02:14 today. The three attempts
   before that failed the same way. The most likely reasons: nas.local was off or asleep at 2am; the
   share name or path changed; the file-server sign-in Backer uses (backup-svc) no longer works.
   Backer will try again at 02:14 tomorrow. You can also try now." Then the Details block and the
   log path.
   - Back it with a dict of nine substrings mapped to plain sentences in `backer.core.messages`:
     `net use` errors 53, 67, 1219 (A2) and 1326; cifs `mount error(13)`; ENOSPC; and the three
     engine strings verified against kopia 0.23.1 - `invalid repository password`, `repository not
     initialized in the provided storage`, and `cannot access storage path`. Those last three are
     the same three the Phase 4 status probe splits on, so the matcher and the probe agree by
     construction. Anything unmatched falls back to "Backer could not finish this backup. The
     details below are the full output from the backup engine." plus the raw text, which is already
     captured and truncated to 5000 characters at `client/agent.py:1006`. Do not build a classifier.
6. Handle the run that never started. `get_background_task_status` (`client/windows_service.py:337`)
   already parses `schtasks /query /fo csv /v`, whose verbose output carries Last Run Time and Last
   Result; read those two columns. Copy: "Windows did not start the scheduled backup at 02:14. The
   computer may have been off or asleep. Backer itself did not run and nothing failed."

### A6 - Health and verification

1. `backer verify JOB` gives three signals of increasing cost, each labelled with what it proves:
   "Last successful backup 14 hours ago (27 Aug 2026, 02:14) / Snapshots 92, oldest 11 Mar 2024 /
   Repository reachable yes / Snapshot contents checking ... 2m 08s ... passed / File contents not
   checked / Restore test not run". Signal 1 is free: `RepositoryMetadata.get_latest_run`
   (`core/repo_metadata.py:519`) with A5's local records as the offline-capable fallback, so the age
   of the last success is available with the share down. Signal 2 is `KopiaBackend.check` after the
   Phase 0 fix replaces `repository validate-client` (`backends/kopia.py:771`, not a kopia 0.23.1
   command) with `snapshot verify`.
2. Be explicit that the cheap check and the real check are different checks, because on this engine
   they are two settings of one command. `snapshot verify` with no `--verify-files-percent` walks
   the snapshot manifests and verifies that every content object is indexed and reachable; it does
   not download or rehash any file. `--verify-files-percent N` randomly downloads and rehashes that
   share of real file content, and it is materially slower than a restic-style
   `check --read-data-subset` because there is no index-only shortcut for the sampled files - the
   bytes come off the share and are hashed. Default `backer verify` to the manifest walk, put the
   sample behind an explicit `--verify-files-percent`, and warn before starting one: "Checking 5% of
   file contents downloads about 1.9 GB from \\nas.local\backups and will take roughly 20 minutes on
   this connection. The check above does not download anything."
3. The green result always names what was not checked, and that sentence is not optional; it is what
   separates this product from one that lies: "Your most recent backup is 14 hours old, and every
   snapshot's contents are indexed and reachable. Backer has not downloaded any of your files to
   confirm they are readable, and has not tried restoring one. To do both: backer verify Documents
   --verify-files-percent 5 --restore-test".
4. `--restore-test` restores the N smallest files from the newest snapshot into a temporary
   directory, hash-compares them against the source where it still exists, and deletes the
   directory. Copy: "Backer restored 12 files from the newest snapshot and they match the files on
   this computer. This tests the repository, not every file in it."
5. Failure leads with what it means for restores, then an ordered remedy: "The backup engine
   reported errors in this repository. This usually means some of the backup data on the file server
   is damaged or missing. Existing snapshots may not restore completely." Then the engine's lines
   verbatim, then: "1. Do not run backer prune on this repository. 2. Check the file server for disk
   problems. 3. Run: backer verify Documents --repair-index. 4. If the errors remain, start a new
   repository and keep this one until the new one has a full backup." Close with the log path from
   A5. `--repair-index` runs `kopia index recover` twice: once without `--commit`, which reports
   what it found and changes nothing, and again with `--commit` after the user confirms that report.
6. Distinguish a timeout from damage. `check` takes `self.config.get("timeout", 3600)`
   (`backends/kopia.py:775`), and a multi-terabyte repository over SMB will exceed an hour and
   surface as a bare `TimeoutExpired`: "The check ran out of time after 1 hour. This does not mean
   the repository is damaged. Large repositories take longer; run it again with --timeout 14400."
7. In the desktop client this is one line per job on Home - "Home NAS - checked 3 days ago" - plus a
   Check this repository button that spawns `backer verify JOB` and warns it can take several
   minutes, and whose Cancel kills that child process rather than orphaning it. Under D8 that is the
   ordinary case rather than a special effort: the client owns the `Process` it started, and the
   CLI's own `finally` disconnects. After 20 seconds any spinner gains an elapsed timer and one line:
   "Large repositories take a while. This is safe to leave running." Elapsed time only, never a
   fabricated percentage.

### A7 - A repository Backer cannot open any more

This is a released regression, not a hypothetical: a repository created before the encryption
passphrase became mandatory has no stored passphrase, and every path that could give it one is
missing. `_build_backup_command_payload` raises before dispatching
(`server/app.py:585-587`), `Storage.get_repository_password` (`server/storage.py:918-924`) does not
fall back to the older column, there is no repository-update endpoint, and
`Storage.set_repository_password` (`server/storage.py:926`) has only test callers. Users on 0.8.0
are hitting this now, so the copy ships whether or not the rest of serverless mode does.

1. What they see, on the job that stops running: "Backer cannot open Home NAS. This repository was
   created by an older version of Backer, which stored its passphrase differently. Backer needs the
   repository passphrase before it can back up to it or restore from it. Nothing on
   \\nas.local\backups has been changed or removed." Never present this as a failed backup - the
   backup did not run, and the distinction is A5 item 6's distinction.
2. What they do: `backer repo recover NAME --passphrase-stdin`. It runs the Phase 4 status probe
   against the supplied passphrase before storing anything, so a wrong one is rejected with A1
   item 6's wording rather than written into the keystore for every later run to fail on. Then:
   "Home NAS opened. Its passphrase is now stored on this computer. The next scheduled backup will
   run normally." This is the production caller `Storage.set_repository_password` has never had.
3. Where the passphrase comes from, said once and plainly, because for some users the honest answer
   is "nowhere": a repository created under a version that used a fixed built-in passphrase can be
   opened by that string, which the release notes for this version publish once for exactly this
   purpose. It is typed into `--passphrase-stdin` like any other passphrase and there is no
   `--legacy-password` flag, so that string appears in no shipped source file and no grep gate has
   to be scoped around it. A repository created under a version that generated a random passphrase
   server-side can be opened only from a backup of that server's database. State which case the user
   is in from the repository record rather than making them guess: "This repository was created on
   2 Nov 2025 by Backer 0.7.2" plus the matching sentence.
4. When neither applies, say so without hedging, and say what still works: "Backer cannot open this
   repository and cannot recover its passphrase. The files on \\nas.local\backups take up space and
   nothing can decrypt them. Create a new repository with: backer repo add NAME --init, and keep the
   old folder until the new one has a full backup of everything you need."
5. This is a one-way door, so it is a command and not an automatic migration. Nothing re-keys a
   repository behind the user's back, and `backer repo recover` never creates a repository: if the
   probe reports `repository not initialized in the provided storage`, it refuses with A1 item 5's
   refusal rather than making an empty one that reports success forever.

Acceptance:

- These three tests lint Python and stay Python's, which is the whole of what the catalogue needs
  now that there is one copy of it.
- `tests/test_messages.py::test_no_exclamation_marks` passes over every string in
  `backer.core.messages` and asserts no string literal ending in an exclamation mark remains in
  `src/backer/cli.py` or anywhere under `src/backer/`. The Tk path it also scanned is deleted.
- `tests/test_messages.py::test_secret_mentions_are_qualified` asserts every displayed catalogue
  string containing `password`, `passphrase` or `sign-in` also contains one of the three exact
  qualifiers `Windows sign-in`, `file-server sign-in` or `repository passphrase`, skipping the A5
  error-matcher keys, which are the engine's and Windows' own text and are matched against, never
  displayed. It proves what the user sees because the user sees this text unmodified (D8).
- `tests/test_messages.py::test_unmatched_error_falls_back_to_raw_output` asserts the nine
  substrings in A5 map to their sentences and that anything else yields the fallback sentence plus
  the raw engine output.
- `tests/test_messages.py::test_engine_is_never_named_outside_a_quote` asserts no displayed
  catalogue string contains `kopia` or `restic` except the three A5 matcher keys, which are quoted
  engine output.
- The client's half is one negative rule and lives in its own project:
  `desktop/Backer.Desktop.Tests::MessagesTests.NoFailureMessageLiterals` (Phase 6 acceptance)
  asserts `desktop/` holds no failure-message literal, so the catalogue cannot acquire a second copy
  by accident. That replaces the earlier draft's "the GUI imports the same module", which a
  non-Python client cannot do.
- `tests/test_cli_serverless.py::test_repo_recover_probes_before_storing` asserts a wrong passphrase
  leaves the keystore untouched and prints A1 item 6's wording, and that a probe result of
  `repository not initialized in the provided storage` refuses rather than creating a repository.

## Appendix B - data formats

### B1 - the unified client config file

One YAML file, `config.yaml`, replaces all four surfaces: `agent.yaml` (`client/agent.py:174-191`),
the deleted Tk GUI's `config.json` (legacy, read by migration only), the `BackerConfig` schema
nothing writes today (`core/config.py:76-106`), and the SYSTEM copy at
`client/windows_service.py:40-51`. YAML because `pyyaml` is already a base dependency
(`pyproject.toml:33`) and `BackerConfig.load`/`save` already round-trip it (`core/config.py:88-99`).

This file is a published contract, not an internal detail, because the desktop client parses it
directly (D8). Two rules follow and are enforced by there being no other implementation: `backer` is
the only process that writes it, and the client reads it read-only. Adding a top-level key is a
compatible change only in the direction of a newer writer and an older reader - and even that fails
loudly, because unknown top-level keys raise rather than being dropped.

| Install | Windows | Linux |
| --- | --- | --- |
| Per-user | `%APPDATA%\Backer\config.yaml` | `$XDG_CONFIG_HOME/backer/config.yaml`, default `~/.config/backer/config.yaml` |
| Machine-scoped | `%ProgramData%\Backer\config.yaml` | `/etc/backer/config.yaml` |

`BACKER_CONFIG_DIR` overrides both and is checked first; resolution order, permissions and migration
are Phase 1's work, and this section defines only the file's contents. Top-level blocks are
`agent_id`, `server`, `repositories`, `jobs`, in that order and no others. That replaces the whole
of today's model: `BackerConfig` carries `version`, `mode`, `server`, `client`, `jobs`, `defaults`,
`log_level` and `log_file` (`core/config.py:79-86`), a `mode` string nothing reads and a `jobs` list
whose entries each require a `destination` (`core/config.py:41-53`). There is no `mode:` key here: a
job naming a repository from `repositories:` is serverless, and a server-managed job arrives in the
heartbeat command payload and is never written to this file at all - the `server:` block sitting
beside serverless jobs is what D5 coexistence looks like on one machine.

There is no `backend` field anywhere in this file, and adding one later is a wire-format break, not
a feature. `DestinationConfig` is now a bare `path` (`core/config.py:18-21`), `JobConfig` has no
`backend_options`, the wire key is `repository_options` (`client/agent.py:895-896`,
`server/app.py:594`), and `tests/test_protocol_contract.py:56-113` rejects `backend`, `backend_type`
and `backend_options` on both the job and the repository API. Kopia is the only engine.

No repository secret appears here; repository credentials are `*_ref` strings holding a keystore key
derived from the record that owns them, never from a hostname or username. Identifiers that are not
credentials stay inline - the SMB `username`, the S3 `access_key_id` - exactly as
`S3Config.public_config` (`backends/s3.py:22-29`) already separates the four public S3 fields from
the two secret ones. The single exception is `server.client_secret`, which stays inline exactly as
`agent.yaml` holds it now until Phase 4 moves it to the keystore. Unknown top-level keys raise a
pydantic `ValidationError` rather than being dropped, so a config written by a newer version fails
loudly instead of silently losing a job. Nothing per-run lives here; `schedule.json`, `runs/`,
`progress/`, `run.lock` and `last_attempt/` live under the data dir.

```yaml
agent_id: 3f9a2c11          # str(uuid4())[:8]; when enrolled, this IS the server's client_id

server:                     # absent entirely on a pure serverless install
  server_url: https://backer.office.example
  client_id: 3f9a2c11
  client_secret: 8f21c0b4d95e17aa   # inline through Phase 1, exactly as agent.yaml holds it;
  heartbeat_interval: 60            # Phase 4 replaces the key with client_secret_ref

repositories:
  home-nas:                 # repository root is //192.168.0.254/Backups/Backer
    name: Home NAS
    type: smb               # v1: smb | local | s3
    server: 192.168.0.254
    share: Backups
    path: Backer            # subpath inside the share
    username: matt
    domain: ""
    scope: machine          # keystore entries the SYSTEM boot task can read
    storage_password_ref: backer/repo/home-nas/storage
    passphrase_ref: backer/repo/home-nas/passphrase
  usb-vault:                # a directory on THIS client, not the server's `local` type
    name: USB Vault
    type: local
    path: E:\BackerVault
    scope: user
    passphrase_ref: backer/repo/usb-vault/passphrase
  offsite:                  # repository root is s3://backer-offsite/matt-desk
    name: Offsite
    type: s3
    bucket: backer-offsite  # the four public fields of S3Config.public_config
    prefix: matt-desk
    endpoint: https://s3.us-west-002.backblazeb2.com
    region: us-west-002
    access_key_id: 002abc1234567890000000001
    scope: user
    storage_password_ref: backer/repo/offsite/storage   # the secret access key
    passphrase_ref: backer/repo/offsite/passphrase

jobs:
  Nightly Documents:
    enabled: true
    repository: home-nas
    source:
      path: C:\Users\matt\Documents
      excludes: ["**/node_modules/**", "*.tmp"]
    schedule:
      cron: "0 2 * * *"     # evaluated locally; the scheduler tick reads only this
    retention: null         # OFF. null or absent means `kopia snapshot expire` is never
                            # invoked for this job. Opt in with keep_last/keep_daily/
                            # keep_weekly/keep_monthly/keep_yearly; this is what a new job gets.
                            # keep_yearly emits kopia's --keep-annual, not --keep-yearly.
  Photos:
    enabled: true
    repository: usb-vault
    source:
      path: C:\Users\matt\Pictures
    schedule: null          # manual only; the scheduler tick never selects it
    retention: null
```

Acceptance:

- `tests/fixtures/config_example.yaml` holds the example above with comments stripped, and NEW
  `tests/test_config_unification.py::test_example_config_round_trips` asserts `BackerConfig.load()`
  accepts it and that `save()` then `load()` yields an equal model.
- NEW `tests/test_config_unification.py::test_no_engine_field_survives_a_round_trip` asserts a
  config carrying `backend`, `backend_type` or `backend_options` at any level raises
  `ValidationError`, matching what `tests/test_protocol_contract.py:56-113` already enforces on the
  wire.
- NEW `tests/test_config_unification.py::test_no_repository_secret_values_in_config` migrates a
  fixture `agent.yaml` plus a legacy `config.json` and asserts the emitted `config.yaml` holds no
  repository passphrase, no SMB password and no S3 secret access key, and that the only inline
  secret is `server.client_secret`, which Phase 4 converts to a ref.
- NEW `tests/test_config_unification.py::test_unknown_top_level_key_rejected` asserts a
  `ValidationError` rather than a silent drop, and `::test_posix_config_is_0600` asserts
  `stat().st_mode & 0o777 == 0o600` after `save()`.

### B2 - the repository `.backer/` sidecar

The sidecar carries run history and everything a second agent needs to adopt a job. It is
authoritative for nothing the local config owns: jobs, schedules, retention and credentials are
settled locally, and progress is never written to the share.

```
<repo>/kopia.repository.f                       <- the kopia repository itself: one per
<repo>/kopia.blobcfg.f                             repository record, shared by every job on
<repo>/kopia.maintenance.f                         it and separated by source (user@host:path)
<repo>/p  q  s  x                                  sharded content, index and manifest blobs
<repo>/_                                           kopia's own log blobs
<repo>/.backer/                                 <- root sidecar, written by serverless agents
  metadata.json                                 repository identity + schema_version
  agents/3f9a2c11.json                          one file per machine
  jobs/Nightly Documents/config.json            get_job_subfolder(job_name)
  jobs/Nightly Documents/runs/20260828T140233Z-3f9a2c11.json
  snapshots/43e8507b6b44.json                   first 12 chars of kopia's 32-hex manifest id
<repo>/Agents/Nightly Documents/                <- a separate kopia repository, written by
<repo>/Agents/Nightly Documents/.backer/           server-managed SMB and NFS runs; both are
                                                   read for discovery and never written by
                                                   the serverless path
```

1. A repository record addresses exactly one kopia repository, at the repository root as this
   machine reaches it. Every job on that record shares it and is separated by kopia's own snapshot
   source, `user@host:path` - a structured field, not a convention: `snapshot list` groups and
   filters on it, `policy set` targets it, and `_find_latest_snapshot_for_source`
   (`backends/kopia.py:386-449`) already reads it from `source.path` (`:417`, `:426`). That is what
   makes per-job retention and per-machine `keep_last` structural rather than something the sidecar
   has to encode. `get_job_subfolder` names sidecar directories only - `jobs/{job_subfolder}/` - and
   never a kopia repository. It moves verbatim from `server/repository_paths.py:6-8` into
   `backer.core.paths` and stays byte-identical to what `server/app.py:575` builds and
   `:600`/`:612`/`:621` concatenate, so the server's importer reads the job document unchanged; its
   substitution class is pinned by `tests/test_workflow_sanity.py:86-89`. Never name a sidecar job
   directory with `RepositoryMetadata._safe_filename` (`core/repo_metadata.py`), which used to
   differ on control characters - that is exactly the drift that would put a job's sidecar under
   one name and the server's copy of it under another, and 0.9 hit it: `config.json` was written
   under `get_job_subfolder` while `runs/` was created under `_safe_filename`, splitting one job
   across two directories. `_safe_filename` now delegates to `get_job_subfolder`, so there is one
   naming function; `_job_dir` still *reads* a pre-0.9 directory when only the legacy name exists
   (`legacy_job_subfolder`), because an existing repository must stay readable. The legacy
   `<repo>/Agents/{job_subfolder}/` trees are separate kopia repositories under the same storage
   root, written today by server-managed SMB and NFS runs only (S3 has no subfolder,
   `server/app.py:637`): read for discovery, never written by the serverless path.
2. One writer per file, by naming, which is what makes cross-machine locking unnecessary.
   `metadata.json` is written once by whichever agent initialises the repository and never
   rewritten; the serverless path never calls `update_metadata()` (`core/repo_metadata.py:310-315`).
   `agents/{agent_id}.json` is written only by the agent whose id names it.
   `jobs/{job_subfolder}/config.json` is written only by the agent named in `owner_agent_id`; an
   adopting agent copies the definition into its own `config.yaml` and never writes back.
   `jobs/{job_subfolder}/runs/{run_id}.json` is write-once. `snapshots/{short_id}.json` is
   *addressed* by content - the 12-character name is the manifest id's prefix, and the full 32-hex
   id stays inside the document (`core/repo_metadata.py::save_snapshot`) - but it is **not**
   content-addressed in the strong sense: the record also carries `run_id`, `hostname` and
   `recorded_at`, which are specific to the writer, so two agents recording the same snapshot do
   not produce identical bytes. It is last-writer-wins, and harmless because a kopia manifest id is
   unique per snapshot creation, so in practice only one agent ever writes a given name. No safety
   property rests on the byte equality this paragraph once claimed.
3. Every write is same-directory temp plus `os.replace`, the temp named `<name>.<agent8>.tmp` so two
   agents cannot collide on the temp file either. Timestamps are UTC ISO 8601 with a trailing `Z`,
   written through the single `core/repo_metadata.py::utc_iso` helper by `initialize`, `save_agent`,
   `save_job`, `save_job_run` and `save_snapshot`, and by the runner's `started_at`/`finished_at`.
   Before 0.9 these were naive local time, unreadable across two machines in different zones;
   records written then are still readable, and `timestamp_key` reads a naive value as UTC so
   ordering between an old and a new record is approximate by up to the old writer's UTC offset.
4. `run_id` is `{utc_compact}-{agent8}`: `%Y%m%dT%H%M%SZ` in UTC, then `agent_id[:8]`. Example
   `20260828T140233Z-3f9a2c11`. It sorts chronologically as a plain string, is unique across
   machines with no allocator, and names a file no other agent can write. An optional third
   component `-{attempt_token}` is appended when `BACKER_ATTEMPT_TOKEN` is set in the environment
   (`serverless/runs.py`): the scheduled-test path sets it and then matches the resulting run by
   `run_id.endswith(token)` (`serverless/scheduled_test.py`), which is the only reason it exists.
   That path regex-validates the token it generates, but an externally set `BACKER_ATTEMPT_TOKEN`
   is not validated and lands verbatim in the `runs/`, `progress/` and `logs/` filenames, so it is
   a developer/test hook rather than a supported knob. Readers must tolerate a run id with two or
   three components. Today
   `client/agent.py:871` falls back to a bare local-time `%Y%m%d_%H%M%S`, so two agents starting
   inside the same second overwrite each other's run record and a DST fold makes two runs an hour
   apart sort identically. A server-supplied `run_id` still wins, which `:871` already prefers.
5. No secret ever appears anywhere in the sidecar - not the repository passphrase, not the SMB
   password, not the S3 secret access key, not a server token, not a keystore ref. The one permitted
   exception is `repository_password_hint`, free text the user types ("1Password -> Home NAS
   kopia"). The writer compares that value against the keystore entry for the repository and against
   the storage password and refuses the write on a match.

Job `config.json`, fattened. The `{job_name, config, created_at, updated_at}` envelope stays exactly
as `save_job` builds it (`core/repo_metadata.py:401-411`), so a server adopts a serverless-created
job record with no server change. It cannot adopt the backup data as-is: the server gives each SMB
and NFS job its own kopia repository under `Agents/{job_subfolder}`, a serverless repository holds
every job at the root separated by source, and nothing converts one into the other. Job records
import; bytes do not. Today the payload is only `{source_path, client_id}`
(`client/agent.py:1584-1590`) - `backend` was removed by `d843681` - which reconstructs nothing:

```json
{
  "schema_version": "2",
  "job_name": "Nightly Documents",
  "owner_agent_id": "3f9a2c11",
  "created_at": "2026-08-28T02:00:14Z",
  "updated_at": "2026-08-29T02:04:52Z",
  "config": {
    "source_path": "C:\\Users\\matt\\Documents",
    "source_hostname": "MATT-DESK",
    "source_platform": "win32",
    "kopia_source": "matt@MATT-DESK:C:\\Users\\matt\\Documents",
    "excludes": ["**/node_modules/**", "*.tmp"],
    "subfolder": "Nightly Documents",
    "schedule": {"cron": "0 2 * * *"},
    "retention": null,
    "repository_hint": {"type": "smb", "server": "192.168.0.254", "share": "Backups", "path": "Backer"},
    "repository_password_hint": "1Password -> Home NAS kopia",
    "client_id": "3f9a2c11"
  }
}
```

`kopia_source` is the `user@host:path` triple exactly as `snapshot list` prints it and `policy set`
takes it. It is the job's identity inside the repository, it is what an adopting agent needs to find
this job's history without guessing, and it is what retention targets - which is why it is recorded
rather than reconstructed. It is not authoritative: an agent that adopts the job on another machine
recomputes its own source and writes its own value. There is no `tags` field, because source
separation covers every v1 case; the one case it cannot cover, two jobs on one folder with different
excludes, is refused at config-validation time rather than encoded here.

`repository_hint` is a hint because the adopting agent may have reached the same bytes another way -
a mapped drive, a different hostname, an existing Linux mount. On adopt it keeps `type` and `path`
and rewrites `server`/`share` from how it actually connected. There is no absolute destination
anywhere in the sidecar: each machine recomputes the repository root from its own `repositories:`
entry, which is what lets one job run from Windows over a UNC path and from Linux over a mount
point.

Run record, written on success and on failure - here the failure case:

```json
{
  "schema_version": "2",
  "run_id": "20260828T140233Z-3f9a2c11",
  "job_name": "Nightly Documents",
  "agent_id": "3f9a2c11",
  "hostname": "MATT-DESK",
  "status": "failed",
  "started_at": "2026-08-28T14:02:33Z",
  "finished_at": "2026-08-28T14:04:01Z",
  "bytes_transferred": 0,
  "files_transferred": 0,
  "snapshot_id": null,
  "error": "prepare_destination: mount //192.168.0.254/Backups failed: NT_STATUS_LOGON_FAILURE",
  "error_stage": "prepare_destination",
  "recorded_at": "2026-08-28T14:04:01Z"
}
```

`status` is `success` or `failed`; `error` is `null` on success and a single-line message otherwise;
`error_stage` is one of `keystore`, `prepare_destination`, `connect`, `backup`, `retention`,
`metadata`, so a pre-flight failure is distinguishable from a backend failure without parsing prose.
`connect` is its own stage because kopia's connect is where wrong-passphrase, unreachable-endpoint
and absent-repository all surface, and today all three are collapsed into an auto-init
(`backends/kopia.py:252-257`).

**Known gap - `error_stage` granularity, and no run record for restores.** The stage list above is
what a reader may encounter, not what every writer can produce. The runner's own sidecar write
(`core/runner.py::_write_repo_metadata`) is reached only after `backend.backup` has returned, so the
stage it records is `backup` or `null` and nothing else. Failures before that point are covered by
`serverless/runs.py::_write_preflight_sidecar_run`, which writes the record from the `finally` block
with the stage it was actually tracking, so `keystore`, `prepare_destination` and `connect` do
appear - best effort, and only where the sidecar is reachable and already initialised (a wrong
passphrase against a local repository writes it; an unreachable share by definition cannot).
`retention` and `metadata` are in the vocabulary but no writer emits them today: a retention failure
lands inside the backup stage's errors, and a metadata failure is the write itself. Restores write
no sidecar record at all, and no local run record either - `backer restore` is not in run history in
any form. Trigger to close this: the first time an operator has to answer "was this data restored,
by whom, from which snapshot" from the repository alone. Until then the plan promises a stage
vocabulary a reader must tolerate, not a guarantee that every stage is reachable.

`snapshot_id` is kopia's full 32-hex manifest id, the value
`backends/kopia.py:342-343` parses from `snapshot create --json` and returns at `:362`.
`bytes_transferred` and `files_transferred` keep the names `client/agent.py:1597-1598` already
writes; both come from `rootEntry.summ` (`backends/kopia.py:336-341`). The run is always recorded
locally under the data dir as well, so an unreachable share still leaves evidence; the sidecar write
stays best-effort and never fails a backup. The agent record is the fields
`client/agent.py:1574-1581` already sends - `agent_id`, `hostname`, `platform`, `os_info` - plus
`schema_version`, `backer_version`, `modes` (`["serverless"]`, `["server"]`, or both), `first_seen`
and `updated_at`.

Acceptance:

- NEW `tests/test_serverless_sidecar.py::test_run_id_unique_per_agent_within_one_second` asserts two
  agents generating a `run_id` at the same frozen instant produce different filenames and that a
  list of them sorts chronologically as plain strings.
- NEW `tests/test_serverless_sidecar.py::test_failed_run_is_recorded` runs a job whose destination
  cannot be prepared and asserts a run record exists with `status == "failed"`, non-null `error`,
  and `error_stage == "prepare_destination"`; `::test_connect_failure_is_its_own_stage` asserts a
  wrong passphrase yields `error_stage == "connect"` and no new repository at the destination.
- NEW `tests/test_serverless_sidecar.py::test_no_secret_in_sidecar` walks every JSON under
  `.backer/` after a backup and asserts no value equals the passphrase, the SMB password or the S3
  secret access key, and no key matches `password|secret|passphrase|token` except
  `repository_password_hint`, and that a hint set to the passphrase is refused at write time.
- NEW `tests/test_serverless_sidecar.py::test_writes_are_temp_then_replace` monkeypatches
  `os.replace` and asserts every sidecar write goes through it with a same-directory temp path and
  that no `.tmp` file survives; `::test_records_carry_schema_version_2` asserts every record under
  `.backer/` reads `"schema_version": "2"`.
- NEW `tests/test_serverless_sidecar.py` gains
  `::test_destination_is_the_repository_root_and_snapshots_carry_the_job_source`, asserting that the
  `--path` argument `_get_repo_type` builds (`backends/kopia.py:129-133`) equals the repository root
  from the `repositories:` entry with no `Agents/` component, that `snapshot list --json` reports
  the job's `source.path` unchanged, that the recorded `kopia_source` matches it, and that the
  sidecar job directory is `jobs/{get_job_subfolder(name)}` for the job name pinned at
  `tests/test_workflow_sanity.py:89`; `python -m pytest -q tests/test_repo_metadata.py` passes
  unchanged.

## Not building

Each refusal is a decision, not an omission. The trigger line states what would reopen it.

1. No serverless daemon. `croniter>=2.0` is already a base dependency (`pyproject.toml:34`), Windows
   already gets a scheduled task driven by `schtasks` (`client/windows_service.py:226`, `:283`), and
   Linux has systemd timers. A resident process adds supervision, restart-on-crash and self-update
   problems to a tool that executes for minutes a day. The scheduler entry invokes the CLI; the CLI
   decides what is due and exits.
   - Trigger: a requirement for sub-minute scheduling or continuous file-change watching.
2. No LAN host discovery. `client/setup_wizard.py:166` already records this call in a comment -
   "Directly prompt for server URL (skip auto-discovery)". A port sweep makes repository identity a
   bare IP address, is indistinguishable from a scanner on a managed network, and answers a question
   the user can answer faster by typing. The user types a hostname, a UNC path or an IP, or picks
   from the short list the OS already knows (`net view` on Windows, existing mounts on Linux).
   Auto-discovery in this document means share and directory enumeration against a host the user
   named, never finding the host itself.
   - Trigger: support traffic showing users cannot name their own NAS. The answer then is mDNS or
     WS-Discovery for names, not a sweep for addresses.
3. No TUI framework (D7). Base dependencies are click, pydantic, rich, pyyaml, croniter, requests
   and tenacity (`pyproject.toml:29-37`) and nothing else. Adding textual is a new runtime
   dependency inside the frozen `backer.exe` build and a third UI surface beside the CLI and the
   desktop client - and the conclusion is stronger now, not weaker: the CLI is what the desktop
   client drives (D8), so every hour spent on a second terminal UI is an hour not spent on the
   surface both front ends depend on. The share picker is a numbered `rich` table with an "up one
   level" entry, and every step has a flag equivalent.
   - Trigger: measured abandonment at the browse step, not a preference for tree widgets.
4. No FUSE or userspace mount layer of Backer's own. Kopia reaches an SMB share only as a filesystem
   path - `_get_repo_type` returns `("filesystem", ["--path", path])` (`backends/kopia.py:129-133`)
   and kopia has no SMB provider - which is why D3 settles transport as `net use` plus a UNC path on
   Windows, `mount -t cifs` on privileged Linux (`client/agent.py:608`), and, on unprivileged Linux,
   the OS's own gvfs mount driven by `gio` (`core/mounts.py`). Shipping a mount layer is what is
   refused: it adds process lifecycle management, a WinFsp prerequisite, and a mount that can die
   mid-run while the backup keeps writing. gvfs adds none of that - it is already installed on every
   desktop, its lifecycle belongs to the session, and Backer neither bundles nor supervises it.
   - Trigger: a matrix cell whose repository has no native filesystem path. By D2 none exists: S3 is
     a kopia provider, not a mount.
5. No rclone in the v1 tool manager (D3). Kopia does have a native rclone provider - `kopia
   repository create rclone --remote-path=...`, verified against the pinned 0.23.1 binary - so the
   restic-era reason this was impossible ("restic addresses a repository by path") is dead, and the
   refusal now rests on cost alone. `TOOL_INFO` ships exactly one binary (`tools/manager.py:20-38`);
   adding rclone means a second checksum-pinned download in `backer setup`, a second process in the
   data path, and a second failure mode in every SMB cell. Mount and UNC already carry every SMB
   cell in D2 and add no binary. The kopia+rclone path is spiked in parallel against the same
   `serverless-smb-linux` and `serverless-smb-windows` tests and adopted in v2 only if it wins on a
   measured axis.
   - Trigger: an SMB cell that mount and UNC cannot make green, or a measured throughput or
     unprivileged-Linux result from the spike. Not a preference for fewer platform branches.
6. No per-tick state on the share. Progress and in-flight state stay under the data dir. Writing
   progress JSON into `.backer/` puts a write on the slowest link in the system several times a
   second, multiplied by every client pointed at that repository. The sidecar carries completed runs
   only.
   - Trigger: none for progress. A cross-machine "what is running now" view would come from `kopia
     snapshot list --all`, which already reports every source's newest snapshot, not from a polled
     progress document.
7. No cross-machine lease. Kopia has no exclusive repository lock to back one with, and does not
   need one: concurrent clients write their own content blobs and their own manifests, so two
   machines backing up to one repository is the supported case rather than a race to arbitrate. What
   is single-writer is maintenance, and kopia already records an owner per repository - `kopia
   maintenance info` prints `Owner: user@hostname`, set to the creating identity and changeable with
   `kopia maintenance set --owner=user@hostname`, both verified against the pinned 0.23.1 binary -
   with `maintenance run --safety=full` as the default that protects an in-flight writer's content
   from another machine's blob GC. The backstop an advisory lease would need is therefore a
   repository setting, not a lease. Use a local `flock`/`msvcrt` lock on the data dir's `run.lock`,
   correct because that path is always a local filesystem, plus `run_id` namespacing so no two runs
   write the same sidecar file, and pin the maintenance owner to the agent that created the
   repository. `KopiaBackend.prune` runs `maintenance run --full` today
   (`backends/kopia.py:709-716`) and sets no owner.
   - Trigger: an operation in the matrix that must be globally exclusive and that kopia's
     maintenance owner does not already serialise.
8. No generic repository-provider abstraction. The server already carries one shape of this -
   `SMBBrowser` (`server/repositories.py:95`), `NFSBrowser` (`:291`), `LocalBrowser` (`:438`) - and
   the v1 client needs two of the three; S3 has nothing to browse, because bucket, prefix, endpoint
   and region are typed, not discovered (`backends/s3.py:22-29`). An interface with two
   implementations is two implementations.
   - Trigger: a third browsable client-side repository type actually ships.
9. No client-side repository-type registry. `server/capabilities.py` was deleted; the server's
   matrix is now the whitelist `{"smb", "nfs", "local", "s3"}` at `server/app.py:4610` and
   `server/web/routes.py:422` plus the per-type dispatch at `server/app.py:597-640`, and it stays
   there. On the client the supported set is a `click.Choice` plus a `Literal` in the config model -
   the same fact in two greppable places, both deletable in one commit when a cell goes red, which
   is exactly what the release gate needs.
   - Trigger: the client cell count outgrowing what a `Choice` renders legibly in `--help`.
10. No batch re-keying of existing repositories. The affected population is repositories created
    before 0.8.0, whose kopia repositories were initialised under the `backer-default-password`
    literal. Their recovery is one repository at a time, driven by the operator, and it is
    store-then-verify-then-rotate: store the literal against the repository record, prove it opens
    with `kopia repository status`, and only then run `kopia repository change-password
    --new-password` (verified as the only re-key command in 0.23.1). Kopia has no `key add` /
    `key remove` pair and no second key, so the rotation is irreversible the instant it runs and
    must never be the step that first tests whether the old password was right. A tool that walks a
    NAS and rewrites keys on repositories it did not create is the largest blast radius in the plan
    aimed at the smallest population.
    - Trigger: a user with enough affected repositories that manual recovery is the reported
      problem. The answer is then a scripted loop over the same three steps, not a different design.
11. No NFS, GCS, Azure or SFTP in the v1 serverless matrix (D2). `_get_repo_type` recognises
    `gs://`, `azure://` and `sftp://` (`backends/kopia.py:106-128`), but no credential plumbing
    exists for
    any of the three and `server/app.py:4610` rejects every repository type outside `{smb, nfs,
    local, s3}`. NFS has a working agent mount path (`client/agent.py:671`) and no CI leg anywhere.
    Each is a cell and each cell needs a passing end-to-end test before it is advertised.
    - Trigger: a green CI leg for that cell. The cell ships in the same commit that turns the leg
      green, never before it.
12. No Android (D1). The phone has neither a storage client nor a repository engine. Every candidate
    either creates a second permanent on-disk format desktop clients cannot read, or adds a four-ABI
    NDK build pipeline this repository has never had. Android stays server-relay-only, and README,
    `--help` and the docs say so in the same words.
    - Trigger: a maintained kopia mobile binding producing the same on-disk repository format.

## Risks

| Risk | Why it bites | Mitigation |
| --- | --- | --- |
| Repositories created before 0.8.0 are already unopenable. This is not a forward risk; it shipped. | The default-password fallback was removed with no migration and no recovery surface. `_build_backup_command_payload` raises when no password is stored (`server/app.py:585-587`); creation requires one for every type (`server/app.py:4612`, helper at `:545-548`); `get_repository_password` has no legacy fallback (`server/storage.py:918-924`, `legacy=False`); the one schema migration promotes `password_encrypted` to `repository_password_encrypted` for `local` and `nfs` only and correctly refuses to for `smb`, where that column held the share credential (`server/storage.py:278-286`); and `Storage.set_repository_password` (`server/storage.py:926`) has test callers only - no endpoint, no CLI, no UI. The literal that opens those repositories appears nowhere under `src/` at HEAD and is documented in neither README nor CHANGELOG. | Ship a way to set the encryption password on an existing repository record - `set_repository_password` gets a production caller - plus the documented store-then-verify-then-rotate procedure from refusal 10, and a CHANGELOG Major Feature bullet naming the affected releases. README quotes the literal verbatim: it shipped in every release before 0.8.0, it is not a secret, and it is the only key to that data. This outranks every other item in this table. |
| Retention is the first code in this product that will delete real backup bytes, and it will do it to snapshots users believe have been pruned for years. | Retention currently deletes nothing at all. `prune` builds `["snapshot", "expire", "--all"]` with no `--delete` (`backends/kopia.py:696`), so kopia only reports, and `prune` returns `success=True`; the server's own local path passes `--delete` (`server/app.py:12705`), so the asymmetry is in-repo evidence. `prune(dry_run=True)` appends a `--dry-run` that is not a flag on `snapshot expire` (`:697-698`) and always errors. `RetentionPolicy` has only ever deleted database rows - `server/retention.py:193` says so. So the fix turns a silent no-op into real deletion against a repository that has accumulated every snapshot ever taken. Worse, the only caller passes no keep arguments (`server/app.py:12717`), so no keep flags are emitted and kopia's built-in global defaults stand: latest 10, hourly 48, daily 7, weekly 4, monthly 24, annual 3. The first run with `--delete` would apply those, not the user's policy, to every source in the repository. | Land the scoping before the deletion, never after: `policy set <user@host:path>` in place of `--global` (`backends/kopia.py:664`) and `snapshot expire <path> --delete` in place of `--all`, in the same commit. Retention defaults to OFF, deleting requires an explicit apply, and the first run of any policy is a preview built from kopia's own `retentionReason` field rather than from a `--dry-run` flag that does not exist. `keep_yearly` is offered nowhere until `KopiaBackend.prune` (`backends/kopia.py:626-634`) takes the parameter and emits `--keep-annual`. |
| Secrets move from one server to N devices. | Today one encrypted server database holds every repository credential. Serverless puts a kopia passphrase, an SMB password and an S3 secret key in a keystore on each desktop, and a dead machine takes its only copy of the key with it with no server to fall back on. Kopia makes a second copy without being asked: `--persist-credentials` defaults on in 0.23.1 and the code passes neither it nor `--no-persist-credentials`, so on Windows a connect can write the passphrase into Credential Manager outside the keystore this plan manages. | OS keystore only, never the config file, scoped `user` or `machine` per repository. Pass `--no-persist-credentials` explicitly on every kopia invocation so there is exactly one copy. Display the passphrase in full once with copy-to-clipboard, require an explicit save confirmation, and verify by reading the secret back out of the keystore before declaring the repository created. |
| Config surface proliferation (D5). | Four client config surfaces exist on disk and the split caused a live bug: the deleted GUI wrote only `config.json` while `_prepare_service_config` (`client/windows_service.py`) looks for `agent.yaml` and raises `FileNotFoundError`. A fifth surface makes failure modes combinatorial, and a second *writer* is worse than a fifth file - which is why D8 gives the desktop client read-only access and routes every mutation through the CLI. | Phase 1 unifies first, with migration. The proof: **the desktop client's install action shells `backer agent install --mode server`, which produces a scheduled task whose run leads to a heartbeat within 90 seconds.** No serverless field is added to any config before that lands. |
| Advertising a matrix cell with no test behind it. | This is not hypothetical: `s3-contract` is mandatory, green, and skipping its only test today, because `tests/test_s3.py:227-234` requires four `BACKER_TEST_S3_*` variables and `release-validation.yml:110-113` supplies three. Windows plus SMB has no leg at all, and the obvious design - a Samba container reached from a `windows-latest` runner - cannot work, because they are different machines. | Phase 7 step 1 repairs the S3 job before anything is built on it, and adds an acceptance bullet asserting `1 passed` rather than `1 skipped`. `New-SmbShare` on the Windows runner itself (Phase 7 step 4), and every new leg in the mandatory loop. If a leg cannot be made green, the cell comes out of README, `--help` and the `Choice()`. |
| Windows packaging: an unsigned installer now carrying two unsigned executables, plus a self-updater that must not repeat the wrong-silent-flag mistake. | No signing step exists in any workflow. The update path previously passed `/S` - an NSIS flag - to an Inno Setup installer, so the "silent" update popped UI and could sit unattended forever; that was fixed to `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART` before the port, and the port moves the whole update-check into the desktop client, where the same mistake is one line away from being made again in a new language. The installer now ships `backer.exe`, `backer-agent-service.exe` and `backer-desktop.exe`, so the unsigned warning covers three binaries. Worse than unsigned-at-rest: the shipped update check in `desktop/Backer.Desktop/ViewModels/SettingsViewModel.cs` downloads that installer over HTTPS into `Path.GetTempPath()` and runs it `/VERYSILENT` with no Authenticode check and no pinned hash, so anything that can write `%TEMP%` between the write and the exec gets a silent elevated install. TLS authenticates the host, not the bytes on disk afterwards. | The client uses the same release-main `backer-agent-setup.exe` URL and the same three Inno flags, both asserted in its own test project now that `tests/test_agent_gui_release_urls.py` is deleted (Phase 7 step 10). The update path additionally downloads to a per-user directory with restrictive ACLs and verifies the file before executing it - the Authenticode signature once signing exists, and until then a SHA-256 published with the release - refusing to run on a mismatch, which is the fail-closed default this product uses everywhere else. Add code signing before serverless is promoted as the recommended install; until then README says plainly that neither the installer nor any of the three binaries is signed. |

## Test plan and release gate

Write every end-to-end test in the real-binary style that already exists in
`tests/test_protocol_contract.py`: `_tool()` (`:383-389`) resolves the binary from `PATH` or
`ToolManager` and calls `pytest.skip` when it is absent, and `_run()` (`:392-398`) asserts on the
return code with stdout and stderr in the message. That file already runs on both platforms through
the `protocol-contract` matrix job at `release-validation.yml:49-75`; serverless gates that need no
service container join it, and the six cells get their own jobs per Phase 7. Every skip guard names
every variable it needs - the S3 job's missing `BACKER_TEST_S3_BUCKET` is what a half-written guard
costs.

1. Phase 0 - `tests/test_backends.py`, which today has no retention test of any kind
   (`TestKopiaBackend` runs `:141-241`; the password guard is `:149-152` and the exclude policy
   `:154-175`): a prune-argv test asserting `policy set` targets the job's `user@host:path` rather
   than `--global` and that `snapshot expire` carries that path and `--delete`, including the
   `--keep-annual` that `KopiaBackend.prune` (`backends/kopia.py:626-634`) has no parameter for; a
   `prune(dry_run=True)` test asserting no `--dry-run` reaches `snapshot expire`; a two-job
   single-repository test where job A's retention runs and job B's snapshots survive; a backup
   against an unreachable destination that fails instead of creating a repository
   (`backends/kopia.py:252-257`), separately for absent, unreachable and wrong-passphrase; a `check`
   test asserting `snapshot verify` rather than the non-existent `repository validate-client`
   (`:771`), and a signature test for the `backend.check(dest, dry_run=dry_run)` call at
   `server/app.py:12775` that is a `TypeError` today; a `KOPIA_CONFIG_PATH` test asserting two
   concurrent operations never share a config file. `tests/test_repo_metadata.py`: `discover_all()`
   on a repository holding both a root sidecar and `Agents/*/` sidecars returns both, deduped by
   `job_name`.
2. Phase 1 - a migration test: a tree containing a legacy `config.json` and `agent.yaml` produces
   one `config.yaml` with every field preserved, `windows_service.py` locates it, and `backer agent
   install --mode server` completes without `FileNotFoundError`. That command is the D5 proof's
   first half: **the desktop client's install action shells `backer agent install --mode server`,
   which produces a scheduled task whose run leads to a heartbeat within 90 seconds** - the test
   covers the command, the Phase 1 acceptance bullet covers the heartbeat.
   `tests/test_windows_packaging.py:79-140` extended for the new filename.
3. Phase 2 - the twelve clean-restore rollback tests at `tests/test_agent_protocol.py:198-546` pass
   against the extracted `backer.core.runner`. Four of them (`:198`, `:229`, `:258`, `:285`)
   currently assert the destination is deleted and the run reported green, which is what
   `client/agent.py:1285-1293` does today when a kopia restore connects and matches nothing, so the
   product and those assertions are corrected in the same commit that extracts the runner - never
   after, because the extraction would carry them forward as a contract. Plus a runner test
   asserting `on_progress` and `on_result` are called with no HTTP client present.
4. Phase 3 - `smb_browse` tests against the CI share: share enumeration returns the share name,
   directory listing returns directories with an is-dir flag, and every command the module emits is
   built with a sentinel password that appears in no argv element.
5. Phase 4 - keystore round trip on both platforms with a headless fallback path; a
   concurrent-writer test that two processes writing `agents/{agent_id}.json` and a run record never
   produce invalid JSON; a due-calculation test across a DST boundary; a test that the SYSTEM task
   and the interactive user resolve the same `run.lock`.
6. Phase 5 - every prompt reachable by flag with stdin closed: a full non-interactive repository
   add, job create and job run under `subprocess` with no TTY, stdin from `/dev/null` on Linux and
   `NUL` on Windows.
7. Phase 6 - `dotnet test desktop/` runs in its own mandatory `desktop-client` job on both
   platforms: source scans for colour literals, dialog count, the `kopia` string and
   failure-message literals, plus ViewModel tests for navigation, the passphrase step and progress
   rendering. No display server is involved and no `xvfb` step exists anywhere any more. UI
   interaction tests under `Avalonia.Headless` are deferred (Phase 6 acceptance), so a green job
   here proves structure and logic, not layout - say so rather than letting the job read as full UI
   coverage.
8. Phase 7 - `tests/test_serverless_e2e.py`, the six D2 cells end to end: init, first backup,
   changed-and-deleted-file backup, snapshot list, restore, failed-restore safety, retention preview
   and `snapshot verify`. Driven by `serverless-local` (matrix `ubuntu-latest`, `windows-latest`),
   `serverless-smb-linux`, `serverless-smb-windows` and `s3-contract` (matrix `ubuntu-latest`,
   `windows-latest`).

Acceptance:

- `python -m pytest -q tests/test_protocol_contract.py tests/test_serverless_e2e.py` passes on both
  `ubuntu-latest` and `windows-latest`, with the SMB and S3 cases self-skipping when any of their
  environment variables is absent.
- The six D2 cells each have a named, passing CI job: `serverless-local (ubuntu-latest)`,
  `serverless-local (windows-latest)`, `serverless-smb-linux`, `serverless-smb-windows`, `Kopia S3
  Contract (ubuntu-latest)` and `Kopia S3 Contract (windows-latest)`, with
  `SERVERLESS_LOCAL_RESULT`, `SERVERLESS_SMB_LINUX_RESULT`, `SERVERLESS_SMB_WINDOWS_RESULT` and the
  existing `S3_CONTRACT_RESULT` all in the mandatory loop at `release-validation.yml:336-342`.
- `grep -rln --include='*.py' "backer-default-password" src/` returns exactly one path, the named
  recovery module - it returns zero today, which is why an affected repository cannot even be
  recognised - and `tests/test_protocol_contract.py::test_rekey_replaces_the_legacy_key` creates a
  kopia repository under the literal, stores it through the new set-password path, asserts `kopia
  repository status` opens it, runs `kopia repository change-password --new-password`, and asserts
  the new passphrase opens it and the literal no longer does; it skips via `_tool("kopia")`
  (`:383-389`) when the binary is absent. README documents the same three steps in the same order.
- Phase 7's `tests/test_workflow_sanity.py::test_cli_choices_match_ci_jobs` bullet is the release
  gate on the matrix, and it is the only definition of that test. When a leg goes red and is not
  fixed, the same commit that accepts the red removes the cell from README, from `backer --help` and
  from the CLI `Choice()`.
