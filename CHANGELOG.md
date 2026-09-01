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

### Minor Features

- Desktop and CLI backup, restore, verification, retention, and repository flows share one serverless configuration model.
- Serverless jobs store their own metadata, credentials, Kopia config, cache, and logs outside the repository.

### Bug Fixes

- Serverless repository creation now pins Kopia maintenance to the creating agent, and Linux SMB operations mount shares instead of invoking Windows networking commands.
- Retention now deletes only expired snapshots for the configured source, reports dry runs without an unsupported flag, and keeps yearly policies.
- Repository verification now reads and rehashes snapshot content through Kopia's supported command.
- Concurrent jobs no longer share or disconnect each other's Kopia configuration.
- Backup never creates a repository from an unreachable path or a wrong passphrase.
- Existing repository records can now store their known encryption passphrase.
- Snapshot file lookup now uses supported Kopia arguments and restores reject same-named source paths from another location.
- Sidecar writes are atomic, failed runs are recorded, and discovery merges root and per-job metadata.
- Clean restores preserve the original destination when no files were restored.
- Retention policy writes no longer arm a later deletion after a dry-run preview.
- The CLI saves agent credentials with its supported call and passes the original source to Kopia restores.
- Agent configuration uses one canonical path across the GUI, interactive client, and service.

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
