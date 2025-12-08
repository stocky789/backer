# Backer

Open-source backup management with web UI - like Veeam/UrBackup but simpler.

**Self-contained**: Backer automatically downloads rclone, restic, and kopia - no manual tool installation required.

**Default Login**: `admin` / `admin` (change after first login)

## Quick Links

- [Releases](https://github.com/stocky789/backer/releases)
- [Report Issues](https://github.com/stocky789/backer/issues)
- [Development Branch](https://github.com/stocky789/backer/tree/dev) - Latest features, may be unstable

## Features

- **Agent-Based Backups**: Deploy agents on Windows/Linux machines for centralized backup management
- **Proxmox VE Integration**: Backup VMs and LXC containers directly from your hypervisor
- **Multiple Backends**: rclone (recommended), restic (encrypted), kopia (modern encrypted)
- **Flexible Storage**: SMB shares, NFS, local paths, S3-compatible storage
- **Web Dashboard**: Monitor agents, jobs, storage usage, and backup history
- **Scheduling**: Cron-based job scheduling with timezone support
- **Retention Policies**: Keep last N, daily, weekly, monthly backups
- **Disaster Recovery**: Auto-import jobs from existing repository metadata

## How It Works

Backer uses a **server + agent** architecture:

- **Server**: Runs on Linux, provides web UI for managing backups
- **Agents**: Run on Windows/Linux machines you want to backup

```
┌─────────────────────────────────────────────────────────────┐
│                    Backer Server + Web UI                   │
│    - Manage agents and hypervisors from web dashboard       │
│    - Configure and schedule backup jobs                     │
│    - Monitor backup status and restore files                │
│    http://your-server:8420                                  │
└─────────────────────────────────────────────────────────────┘
                              │
           ┌──────────────────┼──────────────────┐
           │                  │                  │
           ▼                  ▼                  ▼
    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
    │   Agent     │    │   Agent     │    │  Proxmox    │
    │ (Windows PC)│    │(Linux Server)│   │ Hypervisor  │
    └─────────────┘    └─────────────┘    └─────────────┘
           │                  │                  │
           ▼                  ▼                  ▼
    ┌───────────────────────────────────────────────────────┐
    │     Backup Storage (SMB share, NFS, S3, etc.)         │
    └───────────────────────────────────────────────────────┘
```

---

## Server Installation (Linux)

Install the server on a Linux machine that will manage all your backups.

### Option 1: One-Line Install

```bash
# Install stable release
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/install.sh | sudo bash

# Or install development branch (latest features, may be unstable)
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/install.sh | sudo bash -s -- --branch dev
```

This installs:
- Python environment and dependencies
- Backer server with web UI
- Systemd service (auto-starts on boot)
- Backup tools (rclone, restic, kopia)

**Access Web UI:** `http://your-server:8420`

### Option 2: Docker

**Using pre-built image:**

```bash
docker run -d --name backer \
  -p 8420:8420 \
  -v backer-data:/data \
  --cap-add SYS_ADMIN \
  --security-opt apparmor:unconfined \
  ghcr.io/stocky789/backer:latest
```

**Or using Docker Compose:**

```bash
git clone https://github.com/stocky789/backer.git
cd backer
docker compose up -d
```

> **Note:** `SYS_ADMIN` capability is required for mounting SMB/NFS shares inside the container. If you don't need network storage, you can omit these options.

**Access Web UI:** `http://localhost:8420`

### Option 3: Manual Install

```bash
# Install system dependencies (Debian/Ubuntu)
sudo apt install python3 python3-venv python3-pip cifs-utils smbclient nfs-common

# Clone and setup
git clone https://github.com/stocky789/backer.git
cd backer
python3 -m venv venv
source venv/bin/activate
pip install -e ".[server]"
backer setup
backer server start
```

### Server Management

```bash
# Service commands
sudo systemctl status backer    # Check status
sudo systemctl restart backer   # Restart
sudo systemctl stop backer      # Stop
sudo journalctl -u backer -f    # View logs

# CLI commands
backer server start             # Start server
backer server uninstall         # Uninstall server (with prompts)
backer server uninstall -y      # Uninstall without prompts
backer server uninstall --keep-data  # Uninstall but keep backup data
```

---

## Agent Installation

Install agents on each machine you want to backup.

### Windows Agent

**Option 1: Installer (Recommended)**

1. Go to [Releases](https://github.com/stocky789/backer/releases)
2. Download `backer-agent-setup.exe`
3. Run the installer
4. When the app opens, enter your server URL (e.g., `http://your-server:8420`)
5. Click **Connect**

The agent will:
- Register with your Backer server automatically
- Create a scheduled task to run at startup
- Appear in the Web UI under "Agents"

**Option 2: Portable (No Install)**

1. Download `backer-agent-windows-portable.zip` from Releases
2. Extract to a folder
3. Run `backer-agent.exe`
4. Enter your server URL and click Connect

**Option 3: Install from Python (CLI)**

```powershell
pip install backer[client]
backer agent setup                # Interactive wizard (recommended)
# Or manually:
backer agent register --server http://your-server:8420
backer agent install              # Creates scheduled task
```

### Linux Agent

```bash
# Install stable release
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/scripts/install-agent.sh | sudo bash

# Or install development branch
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/scripts/install-agent.sh | sudo bash -s -- --branch dev
```

This will:
- Install backer to `/opt/backer`
- Run the setup wizard to connect to your server
- Create and start a systemd service

**Agent Commands:**

```bash
backer agent status       # Check connection status
backer agent logs         # View agent logs
backer agent logs -f      # Follow logs in real-time
sudo backer agent setup   # Re-run setup wizard
```

**Service Management:**

```bash
sudo systemctl status backer-agent   # Check status
sudo systemctl restart backer-agent  # Restart
sudo systemctl stop backer-agent     # Stop
```

**Uninstall:**

```bash
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/scripts/install-agent.sh | sudo bash -s -- --uninstall
```

---

## Proxmox VE Integration

Backer can backup VMs and LXC containers directly from Proxmox hypervisors.

### Adding a Hypervisor

1. Go to **Hypervisors** in the web UI
2. Click **Add Hypervisor**
3. Enter your Proxmox host details:
   - Host: `proxmox.example.com` or IP address
   - Port: `8006` (default)
   - Authentication: API token (recommended) or username/password
4. Click **Test Connection** then **Save**

### Creating Hypervisor Backup Jobs

1. Go to **Hypervisors** > select your hypervisor
2. Click **Create Backup Job**
3. Select VMs/containers to backup
4. Choose backup settings:
   - **Mode**: Snapshot (live), Stop, or Suspend
   - **Compression**: None, LZO, GZIP, or ZSTD
   - **Repository**: Where to store backups
5. Set schedule and retention policy
6. Click **Create**

### Features

- **Live Backups**: Snapshot mode for minimal downtime
- **Incremental Backups**: Only backup changed blocks after initial full backup
- **LXC Container Support**: Full backup and restore of containers
- **Auto-Import**: Reconnecting to a repository auto-discovers existing backup jobs
- **Restore**: Restore VMs/containers to any node in the cluster

---

## Web UI Features

Once the server is running, access `http://your-server:8420` to:

- **Dashboard**: Overview of agents, hypervisors, jobs, and storage with statistics
- **Agents**: View connected agents, online/offline status, OS info, and version
- **Hypervisors**: Manage Proxmox connections, VMs, and containers
- **Jobs**: Create and manage backup jobs with cron schedules
- **Repositories**: Configure backup destinations (SMB, NFS, local, S3)
- **History**: View backup run history with detailed logs and progress
- **Restore**: Point-in-time restore with subfolder selection and dry-run option
- **Settings**: Configure timezone and view server information

---

## Backup Backends

| Backend | Status | Best For |
|---------|--------|----------|
| **rclone** | Recommended | Most use cases. Supports SMB, NFS, S3, and 50+ cloud providers. Fast file sync. |
| **restic** | Supported | Encrypted, deduplicated backups with versioning. Select specific snapshots to restore. |
| **kopia** | Supported | Modern encrypted backup with deduplication. Alternative to restic. |

**Note:** Backup tools are automatically downloaded - no manual installation required.

### Restic Encryption

When using restic backend, you can set a custom repository password per backup job. If not specified, a default password is used. **Important:** Remember your password - you'll need it to restore!

---

## Storage Repositories

Backer supports multiple storage types:

| Type | Description |
|------|-------------|
| **SMB/CIFS** | Windows file shares with username/password/domain auth |
| **NFS** | Network File System for Unix/Linux environments |
| **Local** | Direct filesystem paths on the server |
| **S3** | Cloud storage via rclone (AWS S3, MinIO, etc.) |

### Adding a Repository

1. Go to **Repositories** in the web UI
2. Click **Add Repository**
3. Select the type and enter connection details
4. Click **Test Connection** then **Save**

### Scanning for Existing Backups

If you have existing backups in a repository:
1. Go to **Repositories** > select your repository
2. Click **Scan for Backups**
3. Backer will discover existing backup jobs and offer to import them

---

## Standalone Mode (No Server)

For simple single-machine backups without a server:

```bash
pip install backer
backer setup

# Check installed tools
backer tools

# Backup to local drive
backer backup /data /mnt/backup

# Backup to cloud (requires rclone config)
backer backup /data remote:bucket/data

# Dry run first
backer backup /data /backup --dry-run

# With excludes
backer backup /home /backup -e ".cache" -e "node_modules"
```

---

## CLI Reference

```
Setup:
  backer setup              Download and install backup tools
  backer tools              Show installed tools status
  backer backends           Show available backends

Backup (standalone):
  backer backup SRC DEST    Run a backup
  backer restore SRC DEST   Restore from backup

Server:
  backer server start       Start server with web UI
  backer server uninstall   Uninstall server from system

Agent:
  backer agent setup        Interactive setup wizard (recommended)
  backer agent register     Register agent with server (CLI mode)
  backer agent start        Start agent daemon
  backer agent status       Check connection status
  backer agent install      Install as system service/scheduled task
  backer agent uninstall    Remove from system startup
  backer agent logs         View agent logs (-f to follow, -n for line count)

Jobs:
  backer job list           List all jobs
  backer job create         Create a backup job
  backer job run NAME       Run a job manually
```

---

## REST API

The server exposes a REST API at `http://your-server:8420/api/v1/`.

**Authentication:** API endpoints require HTTP Basic Auth with agent credentials (client_id:client_secret) or session cookie from web login.

```bash
# Health check (no auth required)
curl http://localhost:8420/health

# List connected agents
curl http://localhost:8420/api/v1/clients

# List backup jobs
curl http://localhost:8420/api/v1/jobs

# Get scheduler status and upcoming jobs
curl http://localhost:8420/api/v1/scheduler/status

# Run a backup job
curl -X POST http://localhost:8420/api/v1/jobs/my-backup/run

# List hypervisors
curl http://localhost:8420/api/v1/hypervisors

# Get repository storage stats
curl http://localhost:8420/api/v1/repositories/{repo_id}/stats
```

---

## System Requirements

**Server (Linux):**
- Linux (Debian/Ubuntu recommended, also supports RHEL, Arch, openSUSE)
- Python 3.10+
- 512MB RAM minimum
- SMB support: `cifs-utils`, `smbclient`
- NFS support: `nfs-common`

**Windows Agent:**
- Windows 10/11 or Windows Server 2016+
- No Python required (standalone executable available)

**Linux Agent:**
- Python 3.10+ or use install script
- Systemd for service management

---

## Development

For contributors and those who want the latest features:

```bash
# Clone and setup dev environment
git clone -b dev https://github.com/stocky789/backer.git
cd backer
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev,all]"

# Run tests
make test

# Lint and format
make lint
make format

# Run server in dev mode
backer server start
```

### Project Structure

```
src/backer/
├── agent/          # Windows/Linux agent with GUI
├── backends/       # Backup backends (rclone, restic, kopia)
├── client/         # Agent client code
├── core/           # Shared core functionality
├── hypervisors/    # Proxmox VE integration
│   ├── proxmox.py  # Proxmox API client
│   ├── qmp.py      # QEMU Machine Protocol
│   └── metadata.py # Backup metadata handling
└── server/
    ├── app.py      # FastAPI application
    ├── storage.py  # SQLite database layer
    ├── scheduler.py # Cron scheduler
    ├── retention.py # Backup retention policies
    └── web/        # Web UI templates
```

### Creating a Release

```bash
# Update version and create tag
make release VERSION=X.Y.Z

# Push to GitHub (triggers automated build)
git push && git push --tags
```

CI/CD Pipeline:
1. Runs tests on Linux and Windows
2. Builds Windows agent installer (`.exe`)
3. Builds Docker image and pushes to `ghcr.io/stocky789/backer`
4. Builds Python packages (`.whl`, `.tar.gz`)
5. Creates GitHub release with all artifacts

### Development Branch

The `dev` branch contains the latest features and improvements. Use `--branch dev` with install scripts to get bleeding-edge updates:

```bash
# Server
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/install.sh | sudo bash -s -- --branch dev

# Agent
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/scripts/install-agent.sh | sudo bash -s -- --branch dev
```

---

## License

MIT
