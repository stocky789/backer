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

## 0.8.0

### Major Features

- **Directory backups now use Kopia snapshots exclusively.** Repository and job setup no longer offers an engine choice.

### Minor Features

- S3-compatible repositories are supported with native Kopia configuration.
- Each repository has its own encryption password.

### Bug Fixes

- SMB and NFS repository setup now requests the encryption password before connection testing.
- Windows agents now recognize S3 repository URLs instead of treating them as NFS paths.
- Job exclusion patterns now use Kopia source policies compatible with current Kopia releases.

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
