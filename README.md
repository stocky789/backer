# Backer

Unified backup orchestration - consolidating rsync, rclone, restic and more into one tool.

## Overview

Backer is an open-source backup solution that provides a unified interface for multiple backup backends. Instead of reinventing the wheel, it leverages battle-tested tools:

- **rsync** - Fast file synchronization
- **rclone** - Cloud storage (S3, Google Drive, Backblaze B2, etc.)
- **restic** - Deduplicated, encrypted backups with snapshots
- **borgbackup** - Similar to restic (coming soon)

## Features

- **Unified CLI** - One command to rule them all
- **Multiple backends** - Use the right tool for each job
- **Client-server architecture** - Central management of distributed backups
- **REST API** - Easy integration and web UI ready
- **Job scheduling** - Cron-based scheduling (coming soon)
- **Retention policies** - Automatic cleanup of old backups

## Installation

```bash
# Basic installation (CLI only)
pip install backer

# With server support
pip install backer[server]

# With client/agent support
pip install backer[client]

# Everything
pip install backer[all]
```

## Quick Start

### Standalone Usage

Run a one-off backup:

```bash
# Using rsync (default)
backer backup /home/user/documents /mnt/backup/documents

# Using rclone to cloud storage
backer backup /data remote:bucket/data -b rclone

# Using restic with encryption
backer backup /home/user restic:/mnt/backup/home -b restic

# Dry run to see what would happen
backer backup /data /backup -n
```

Restore from backup:

```bash
backer restore /mnt/backup/documents /home/user/documents

# Restore specific snapshot (restic)
backer restore restic:/backup /restore -b restic -s abc123
```

Check available backends:

```bash
backer backends
```

### Client-Server Mode

For managing backups across multiple machines:

**On the server:**

```bash
# Start the backup server
backer server start --port 8420
```

**On client machines:**

```bash
# Register with the server
backer agent register --server http://backup-server:8420

# Start the agent daemon
backer agent start
```

**Managing jobs:**

```bash
# Create a backup job
backer job create \
  --name daily-docs \
  --source /home/user/documents \
  --dest /mnt/backup/docs \
  --schedule "0 2 * * *"

# List all jobs
backer job list

# Run a job manually
backer job run daily-docs
```

## Configuration

Configuration is stored in `~/.config/backer/config.yaml`:

```yaml
version: "1"
mode: standalone  # or "server" / "client"

jobs:
  - name: documents
    source:
      path: /home/user/documents
      excludes:
        - "*.tmp"
        - ".cache"
        - "node_modules"
    destination:
      path: /mnt/backup/documents
      backend: rsync
    schedule:
      cron: "0 2 * * *"  # Daily at 2 AM
    retention:
      keep_daily: 7
      keep_weekly: 4
      keep_monthly: 12
```

## API

The server exposes a REST API at `http://localhost:8420`:

- `GET /health` - Health check
- `GET /api/v1/clients` - List registered clients
- `POST /api/v1/clients/register` - Register a new client
- `GET /api/v1/jobs` - List backup jobs
- `POST /api/v1/jobs` - Create a job
- `POST /api/v1/jobs/{name}/run` - Run a job
- `GET /api/v1/jobs/{name}/runs` - Get job history

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Backer Server                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │  REST API   │  │  Scheduler  │  │  Storage (SQLite)   │ │
│  └─────────────┘  └─────────────┘  └─────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
        │                    │                    │
        ▼                    ▼                    ▼
┌───────────────┐  ┌───────────────┐    ┌───────────────┐
│ Backer Agent  │  │ Backer Agent  │    │ Backer Agent  │
│  (client 1)   │  │  (client 2)   │    │  (client N)   │
└───────────────┘  └───────────────┘    └───────────────┘
        │                    │                    │
        ▼                    ▼                    ▼
┌───────────────────────────────────────────────────────────┐
│              Backend Abstraction Layer                     │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────────┐  │
│  │  rsync  │  │ rclone  │  │ restic  │  │ borgbackup  │  │
│  └─────────┘  └─────────┘  └─────────┘  └─────────────┘  │
└───────────────────────────────────────────────────────────┘
```

## Development

```bash
# Clone and install in development mode
git clone https://github.com/stocky789/backer.git
cd backer
pip install -e ".[dev]"

# Run tests
pytest

# Format and lint
ruff check --fix .
ruff format .
```

## License

MIT License - see LICENSE file.

## Roadmap

- [ ] Borgbackup backend
- [ ] Web UI dashboard
- [ ] Email notifications
- [ ] VM snapshot support (libvirt/QEMU)
- [ ] Windows support
- [ ] Encryption at rest
- [ ] Bandwidth throttling
- [ ] Deduplication stats dashboard
