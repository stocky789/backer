# Changelog

Notable changes per release. Older history is in `git log`.

**Format rule:** every release section uses the same three subsections, in this order,
and omits any that would be empty:

```
## <version>

### Major Features
### Minor Features
### Bug Fixes
```

Major Features are changes that alter how Backer is installed, secured, or operated.
Minor Features are additions and adjustments that fit into the existing workflow.
Bug Fixes are corrections to behaviour that was already meant to work.
The release workflow publishes the top section verbatim as the release notes.

## 0.9.0

### Major Features

- Serverless backups run directly from Linux and Windows clients to local directories, SMB shares, and S3-compatible storage.
- Operators can recover repositories orphaned by 0.8.0: set the existing repository passphrase, verify from a fresh config, rotate it, then verify again. A lost passphrase cannot be recovered.
- Breaking: the Python Tk desktop GUI is removed and replaced by the Backer desktop client, one cross-platform C#/Avalonia app for Windows and Linux. It reads the agent configuration read-only and performs every change by running the `backer` CLI.
- The frozen Windows Python artifact is now the console CLI `backer.exe`; the installer ships it alongside `backer-desktop.exe` and the unattended service executable, under the same `backer-agent-setup.exe` download name.

### Minor Features

- Desktop and CLI backup, restore, verification, retention, and repository flows share one serverless configuration model.
- Agent configuration now stores only identity, server credentials, repositories, and jobs; scheduler pauses live with local runtime state.
- Serverless jobs store their own metadata, credentials, Kopia config, cache, and logs outside the repository.
- New CLI commands cover what the GUI used to own: `backer schedule pause/resume/show/status`, `backer job set`, `backer job run --json`, and `backer keystore status --json`.
- Confirmations that previously needed a terminal can now be carried by a flag for non-interactive callers (`restore --into REPLACE --yes-replace`, `repo rm --yes --confirm-name`, `verify --repair-index --yes`); without the flag a non-TTY still refuses.
- The `client` extra no longer installs `pystray` or `pillow`.
- Windows share discovery works without smbclient, using `net view`, including share names that contain spaces.
- Desktop Linux mounts SMB shares through gvfs, so backups, restores and scheduled runs no longer need root; an already-mounted share is reused as-is.
- The backup progress display shows a real percentage and live transfer speed, and keeps moving through the upload phase (when Kopia has finished reading files and is flushing them to the destination).

### Bug Fixes

- The backup run no longer looks stalled while it is really uploading: progress now tracks bytes flushed to the destination, not only bytes hashed, and the run log streams live instead of appearing only when the run ends.
- Creating a repository (`--init`) now creates the destination folder inside a share if it does not exist, instead of reporting the missing folder as unreachable storage.
- Adding a repository where one already exists now says so plainly, and a wrong passphrase for an existing repository is reported as such, rather than surfacing Kopia's raw "invalid repository password".
- Configured job excludes were not applied by serverless backups: every excluded file was backed up anyway, on every run, with no error. Excludes now take effect, and are checked end to end against the pinned Kopia binary.
- SMB share discovery and browsing work on hosts without `/etc/samba/smb.conf`, failed sign-ins are reported as such instead of an unknown error, and share discovery accepts a domain.
- Creating an SMB repository without root on Linux is refused with the supported alternatives named, instead of failing with a mount traceback.
- Source distributions exclude local agent workspace artifacts.
- Serverless repository creation now pins Kopia maintenance to the creating agent, and Linux SMB operations mount shares instead of invoking Windows networking commands.
- Retention now deletes only expired snapshots for the configured source, reports dry runs without an unsupported flag, and keeps yearly policies.
- Repository verification now reads and rehashes snapshot content through Kopia's supported command.
- Concurrent jobs no longer share or disconnect each other's Kopia configuration.
- Backup never creates a repository from an unreachable path or a wrong passphrase.
- Existing repository records can now store their known encryption passphrase.
- Snapshot file lookup now uses supported Kopia arguments and restores reject same-named source paths from another location.
- Sidecar writes are atomic, failed runs are recorded, and discovery merges root and per-job metadata.
- A machine that adopted a job now records its runs and snapshots in the repository sidecar. Previously the whole metadata write was abandoned because the adopting machine is not the job's owner, so a replacement machine's backups left no trace in the repository and a later adopter saw only the original machine's history.
- Sidecar timestamps are UTC with a trailing `Z` instead of the writing machine's naive local time, so run history from two machines sorts correctly; records written by older Backer versions stay readable and are ordered as best they can be.
- A sidecar job's `config.json` and its `runs/` directory always share one name, and a job document is no longer rewritten when nothing about the job changed.
- `backer repo adopt` refuses to overwrite a local job of the same name unless `--replace-existing` is given, warns when the adopted source path does not exist on this machine (naming the recorded host, the stored repository hint and the `--source NAME=PATH` remap), adopts a disabled job as disabled, refuses a sidecar written by a newer Backer, and with `--all` imports every job it can while reporting the ones it could not and exiting non-zero. Its `--json` output is now an object with `adopted`, `warnings` and `failures` instead of a bare list.
- Clean restores preserve the original destination when no files were restored.
- Desktop REPLACE restores require typed confirmation and retain the original destination in a `.replaced-*` folder.
- Server-managed retention previews remain non-mutating; serverless previews save the scoped policy before confirmation and list Kopia-identified expired snapshots, SMB retention loads credentials before mounting, and applied deletion counts are accurate.
- The CLI saves agent credentials with its supported call and passes the original source to Kopia restores.
- Linux restores no longer refuse every destination because `/` is treated as an ancestor of all paths.
- Windows restores refuse a drive root such as `C:\` and the `C:\Users` folder itself, and cover `Program Files (x86)`.
- Agent configuration uses one canonical path across the desktop client, interactive client, and service.
- Short snapshot IDs stay connected while their full Kopia IDs are resolved, and sidecar agent/job records now use atomic v2 writes.
- Retention previews request Kopia's retention reasons, non-interactive repository setup requires explicit headless mode, and scheduler fires and pause state no longer overwrite each other.
- Repository run records now include result codes and a safe failure summary.
- Sidecar job documents now drop Windows-style absolute repository paths on Linux.
- Local run records carry the backend result again, so `job history`, `job status` and the desktop client report real bytes, files and duration instead of nothing.
- Progress documents no longer report a percentage against the previous snapshot's size: when a grown source passes that estimate, `total_bytes` and `progress_percent` are `null`, and a derived percentage never exceeds 99 before the final frame.
- A run aborted because the repository at the configured location is not the one the job was added against now says so, naming the expected and found repository ids.
- Pre-flight failures (wrong passphrase, unreachable destination) are recorded in the repository sidecar when it is reachable, not only in local state.
- Ctrl-C during `backer job run` stops Kopia, records a cancelled run and exits 130.
- `backer job run --json` writes only the run id line and the result line to stdout; all narration goes to stderr.
- `backer repo adopt` no longer crashes on an S3 repository configured without a prefix.
- Run logs keep the end of the output, where Kopia's error is, and rotate 20 logs per job instead of 20 across all jobs.
- The duplicate-source refusal prints the source path instead of a Python tuple, and `backer snapshots <repository>` explains the job-versus-repository mix-up instead of raising a traceback.

## 0.8.0

### Major Features

- **Directory backups now use Kopia snapshots exclusively.** Repository and job setup no longer offers an engine choice.

### Minor Features

- S3-compatible repositories are supported with native Kopia configuration.
- Each repository has its own encryption password.

### Bug Fixes

- Repository creation now rejects unsupported repository types and requires an explicit encryption password before any repository is created.
- Imported and newly written repository metadata no longer carries obsolete backup-engine fields.
- SMB and NFS repository setup now requests the encryption password before connection testing.
- Windows agents now recognize S3 repository URLs instead of treating them as NFS paths.
- Job exclusion patterns now use Kopia source policies compatible with current Kopia releases.
- Updating or removing a job exclusion now replaces the previous Kopia ignore policy.
- History live updates no longer request a missing runs endpoint in browsers.

## 0.7.2

### Major Features

- **First-run setup wizard replaces the built-in `admin`/`admin` account.** A new server has no users; every page except `/setup`, `/health`, and static files redirects there, and all `/api/` calls return 503 until an owner account is created. The first admin is created atomically, so a race cannot produce two owners.
- **New agents require a single-use enrollment key.** Keys are generated from the Agents page inside each platform's setup card (no longer buried in Settings) as short typable codes (`XXXX-XXXX`, no look-alike characters) that expire after 15 minutes and are consumed by one registration, so they can be entered by hand on a phone. The Linux CLI, Windows agent GUI, and Android app all send one.
- **Management APIs are no longer public.** Everything under `/api/` requires a browser session or admin HTTP Basic credentials. Only endpoints that authenticate agents themselves are exempt, matched by exact method and path (register, token, heartbeat, results, progress, command ack, browse results, and the proxy repository API) rather than by an `/api/v1/` prefix.
- **Agent re-registration requires the existing agent's credentials.** Previously any caller that knew a hostname could re-register it and be handed fresh credentials.
- **Passwords now use PBKDF2-HMAC-SHA256** (600,000 iterations, per-password salt). Existing salted SHA-256 hashes still verify and are silently upgraded on the next successful login.

### Minor Features

- The setup wizard also collects timezone and the public URL agents use, validating each field.
- `BACKER_PUBLIC_URL` is gone. The public URL is set in the wizard and editable in Settings, so it is no longer silently auto-detected at startup.
- Session cookies are marked `Secure` when the server is reached over HTTPS.
- `--token` / `BACKER_ENROLLMENT_TOKEN` on `backer agent register` and `backer agent start`.
- `--username` / `--password` (or `BACKER_ADMIN_USERNAME` / `BACKER_API_PASSWORD`) on `backer job list|create|run` and `backer agent progress`, needed now that those APIs authenticate.
- Android setup screen has an enrollment key field; Register stays disabled until it is filled.
- `docker-compose.yml` builds the image locally rather than pulling `ghcr.io/stocky789/backer:latest`.
- Repository, installer, and updater URLs point at `git.stockhome.com.au` instead of GitHub.
- This changelog; release notes are taken from its top section instead of a generic commit line.

### Bug Fixes

- Restic clean restores now pin `latest` to one snapshot and roll back instead of replacing the destination when no snapshot items match.
- The JWT signing secret was regenerated on every call, so freshly issued agent tokens could not be verified. It is now cached for the process lifetime.
- Proxy backend no longer rewrites `proxys://host/repo/x` to port 8421: an omitted port means the scheme's default, and an explicit port is preserved.
- The login page no longer advertises default credentials, and dropdown options are readable on the dark login and setup pages.

## 0.7.1

### Minor Features

- Windows agent executable now carries version metadata.
- Agents use self-scheduling continuous heartbeat polling.
- Android APK build and release added to CI, with Backer branding and a custom launcher icon.
- Releases are published from successful main branch builds.

### Bug Fixes

- Fixed Windows SMB repository initialization and restic staging in the installer.
- Fixed server errors on the login page.
- Fixed Android release builds (shrinker rules).
