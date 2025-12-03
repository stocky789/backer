# Backer

Unified backup orchestration - one tool to manage rsync, rclone, restic and more.

## Prerequisites

You need at least one of these backup tools installed:

```bash
# Debian/Ubuntu
sudo apt install rsync rclone restic

# Arch
sudo pacman -S rsync rclone restic

# macOS
brew install rsync rclone restic
```

## Installation

```bash
# Clone the repo
git clone https://github.com/stocky789/backer.git
cd backer

# Install in development mode (recommended for now)
pip install -e ".[all]"

# Or just the basics (no server/client)
pip install -e .
```

Verify it works:

```bash
backer --version
backer backends    # Shows which backup tools are available
```

## Usage

### 1. Simple Backups (Standalone Mode)

No server needed - just run backups directly.

**Backup a folder with rsync:**

```bash
# Basic backup
backer backup /home/user/documents /mnt/external/documents

# Dry run first (see what would happen)
backer backup /home/user/documents /mnt/external/documents --dry-run

# Exclude patterns
backer backup /home/user/projects /backup/projects \
  -e "node_modules" \
  -e "*.log" \
  -e ".git"
```

**Backup to cloud with rclone:**

First configure rclone if you haven't:

```bash
rclone config   # Interactive setup for your cloud provider
```

Then backup:

```bash
# Backup to configured remote (e.g., "gdrive" or "s3")
backer backup /home/user/photos gdrive:backups/photos -b rclone

# Backup to S3-compatible storage
backer backup /data s3:mybucket/data -b rclone
```

**Backup with restic (encrypted + deduplicated):**

First initialize a restic repo:

```bash
export RESTIC_PASSWORD="your-secure-password"
restic init -r /mnt/backup/restic-repo
```

Then backup:

```bash
backer backup /home/user /mnt/backup/restic-repo -b restic
```

**Restore:**

```bash
# Restore from rsync backup
backer restore /mnt/external/documents /home/user/documents-restored

# Restore from restic (latest snapshot)
backer restore /mnt/backup/restic-repo /tmp/restored -b restic

# Restore specific restic snapshot
backer restore /mnt/backup/restic-repo /tmp/restored -b restic -s abc123
```

### 2. Server Mode (Multi-Machine Backups)

For backing up multiple machines from a central server.

**Start the server:**

```bash
# On your backup server
backer server start

# Custom port
backer server start --port 9000

# Bind to specific interface
backer server start --host 192.168.1.100 --port 8420
```

The server runs at `http://localhost:8420` by default.

**Register client machines:**

```bash
# On each machine you want to back up
backer agent register --server http://backup-server:8420

# This saves credentials to ~/.config/backer/agent.yaml
```

**Start the agent:**

```bash
# Run in foreground
backer agent start

# Or check status
backer agent status
```

**Create and manage jobs:**

```bash
# Create a backup job
backer job create \
  --name workstation-docs \
  --source /home/user/documents \
  --dest /mnt/backup/workstation/docs \
  --backend rsync \
  --server http://backup-server:8420

# List all jobs
backer job list --server http://backup-server:8420

# Run a job manually
backer job run workstation-docs --server http://backup-server:8420

# Dry run
backer job run workstation-docs --dry-run --server http://backup-server:8420
```

### 3. Using the API Directly

The server exposes a REST API you can use with curl or any HTTP client:

```bash
# Health check
curl http://localhost:8420/health

# List all clients
curl http://localhost:8420/api/v1/clients

# List all jobs
curl http://localhost:8420/api/v1/jobs

# Create a job
curl -X POST http://localhost:8420/api/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-backup",
    "source_path": "/data",
    "destination_path": "/backup/data",
    "backend": "rsync"
  }'

# Run a job
curl -X POST http://localhost:8420/api/v1/jobs/my-backup/run \
  -H "Content-Type: application/json" \
  -d '{"dry_run": false}'

# Get job history
curl http://localhost:8420/api/v1/jobs/my-backup/runs
```

## CLI Reference

```
backer --help                    Show all commands
backer backup --help             Backup command options
backer restore --help            Restore command options
backer backends                  List available backends

backer server start              Start backup server
backer server start -p 9000      Custom port

backer agent register -s URL     Register with server
backer agent start               Start agent daemon
backer agent status              Check agent status

backer job list                  List all jobs
backer job create                Create a job
backer job run NAME              Run a job
```

## Example: Full Backup Setup

Here's a complete example backing up a home directory to an external drive:

```bash
# 1. Check rsync is available
backer backends

# 2. Do a dry run first
backer backup /home/user /mnt/external/home-backup \
  -e ".cache" \
  -e "Downloads" \
  -e ".local/share/Trash" \
  --dry-run

# 3. Run the actual backup
backer backup /home/user /mnt/external/home-backup \
  -e ".cache" \
  -e "Downloads" \
  -e ".local/share/Trash"

# 4. Verify with verbose output
backer backup /home/user /mnt/external/home-backup \
  -e ".cache" \
  --verbose
```

## Data Storage

- **Server database:** `~/.local/share/backer/backer.db` (SQLite)
- **Job history:** `~/.local/share/backer/history/`
- **Agent config:** `~/.config/backer/agent.yaml`

## Current Limitations

This is early development. What works:

- Single backup/restore operations via CLI
- Server API for job management
- Client registration and heartbeat
- rsync, rclone, restic backends

What's not implemented yet:

- Scheduled jobs (cron defined but not executing)
- Automatic agent job execution from server
- Web UI
- Email notifications

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run linter
ruff check src/

# Format code
ruff format src/

# Type check
mypy src/backer/
```

## License

MIT
