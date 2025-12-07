# Backer (Development Branch)

> ⚠️ **This is the `dev` branch** - contains unstable/experimental features. For stable releases, see the [main branch](https://github.com/stocky789/backer/tree/main).

Open-source backup management with web UI - like Veeam/UrBackup but simpler.

**Self-contained**: Backer automatically downloads rclone, restic, and kopia - no manual tool installation required.

**Default Login**: `admin` / `admin` (change after first login)

## Quick Links

- 📦 [Stable Releases](https://github.com/stocky789/backer/releases)
- 🐛 [Report Issues](https://github.com/stocky789/backer/issues)
- 📝 [Main Branch README](https://github.com/stocky789/backer/blob/main/README.md)
- 🔧 [Development Setup](#development-setup)

## How It Works

Backer uses a **server + agent** architecture:

- **Server**: Runs on Linux, provides web UI for managing backups
- **Agents**: Run on Windows/Linux machines you want to backup

```
┌─────────────────────────────────────────────────────────────┐
│                    Backer Server + Web UI                   │
│    - Manage agents from web dashboard                       │
│    - Configure and schedule backup jobs                     │
│    - Monitor backup status and restore files                │
│    http://your-server:8420                                  │
└─────────────────────────────────────────────────────────────┘
                              │
           ┌──────────────────┼──────────────────┐
           │                  │                  │
           ▼                  ▼                  ▼
    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
    │   Agent     │    │   Agent     │    │   Agent     │
    │ (Windows PC)│    │(Linux Server)│   │   (NAS)     │
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

### Option 1: One-Line Install (Dev Branch)

```bash
# Install from dev branch (latest unstable)
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/dev/install.sh | sudo bash -s -- --branch dev

# Or for stable release:
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/install.sh | sudo bash
```

This installs:
- Python environment and dependencies
- Backer server with web UI
- Systemd service (auto-starts on boot)
- Backup tools (rclone, restic)

**Access Web UI:** `http://your-server:8420`

### Option 2: Docker (Recommended for Containers)

**Using pre-built image:**

```bash
docker run -d --name backer \
  -p 8420:8420 \
  -v backer-data:/data \
  --cap-add SYS_ADMIN \
  --security-opt apparmor:unconfined \
  ghcr.io/stocky789/backer:latest
```

**Or using Docker Compose (dev branch):**

```bash
git clone -b dev https://github.com/stocky789/backer.git
cd backer
docker compose up -d
```

> **Note:** `SYS_ADMIN` capability is required for mounting SMB/NFS shares inside the container. If you don't need network storage, you can omit these options.

**Access Web UI:** `http://localhost:8420`

### Option 3: Manual Install (Dev Branch)

```bash
# Install system dependencies (Debian/Ubuntu)
sudo apt install python3 python3-venv python3-pip cifs-utils smbclient nfs-common

# Clone dev branch and setup
git clone -b dev https://github.com/stocky789/backer.git
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

### Server Uninstall

To completely remove the server:

```bash
sudo backer server uninstall
```

Or use the standalone script (useful if CLI is broken):

```bash
sudo ./scripts/uninstall-server.sh
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

**Uninstall Windows Agent:**

Use Windows Settings > Apps > Backer Agent, or run the installer again and choose Uninstall.

### Linux Agent

```bash
# Install from dev branch
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/dev/scripts/install-agent.sh | sudo bash -s -- --branch dev

# Or stable:
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/scripts/install-agent.sh | sudo bash
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
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/dev/scripts/install-agent.sh | sudo bash -s -- --uninstall
```

---

## Web UI Features

Once the server is running, access `http://your-server:8420` to:

- **Dashboard**: Overview of all backups and agent status with statistics
- **Agents**: View connected agents, online/offline status, OS info, and version
- **Jobs**: Create and manage backup jobs with cron schedules
- **Storage**: Configure backup destinations (SMB shares, NFS, local paths, S3 via rclone)
- **History**: View backup run history with detailed logs and progress
- **Restore**: Point-in-time restore with subfolder selection and dry-run option
- **Settings**: Configure timezone and view server information
- **Profile**: Manage your account, change password

---

## Backup Backends

| Backend | Status | Best For |
|---------|--------|----------|
| **rclone** | ✅ Recommended | Most use cases. Supports SMB, NFS, S3, and 50+ cloud providers. Fast file sync. |
| **restic** | ✅ Supported | Encrypted, deduplicated backups with versioning. Select specific snapshots to restore. |
| **kopia** | ✅ Supported | Modern encrypted backup with deduplication. Alternative to restic. |
| **rsync** | ❌ Not Available | Not supported for agent-based backups. Use rclone instead. |

**Note:** Backup tools (rclone, restic, kopia) are automatically downloaded - no manual installation required.

### Restic Encryption

When using restic backend, you can set a custom repository password per backup job. If not specified, a default password is used. **Important:** Remember your password - you'll need it to restore!

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

## API

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
curl http://localhost:8420/api/v1/jobs/my-backup/run -X POST

# Trigger a restore
curl http://localhost:8420/api/v1/restore -X POST \
  -H "Content-Type: application/json" \
  -d '{"job_name": "my-backup", "client_id": "agent-uuid"}'
```

See the full API by browsing the server routes in the source code.

---

## System Requirements

**Server (Linux):**
- Linux (Debian/Ubuntu recommended)
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

## Development Setup

### Prerequisites

```bash
# Debian/Ubuntu
sudo apt install python3 python3-venv python3-pip cifs-utils smbclient nfs-common sshpass

# Arch Linux
sudo pacman -S python python-pip cifs-utils smbclient nfs-utils sshpass
```

### Clone and Setup

```bash
# Clone dev branch
git clone -b dev https://github.com/stocky789/backer.git
cd backer

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install in development mode with all extras
pip install -e ".[dev,all]"
```

### Running Locally

```bash
# Start server in foreground (for development)
backer server start

# Or run directly with Python for debugging
python -m backer.server.daemon

# Run agent (in another terminal)
backer agent start
```

### Testing

```bash
# Run all tests
make test

# Run specific test file
pytest tests/test_storage.py -v

# Run with coverage
pytest --cov=backer tests/
```

### Code Quality

```bash
# Lint (ruff)
make lint

# Format code
make format

# Type checking
make typecheck

# Run all checks
make check
```

### Project Structure

```
src/backer/
├── agent/          # Windows/Linux agent with GUI
├── backends/       # Backup backends (rclone, restic, kopia)
├── client/         # Agent client code
├── core/           # Shared core functionality
├── hypervisors/    # Proxmox/VMware integration
│   ├── proxmox.py  # Proxmox API client
│   ├── qmp.py      # QEMU Machine Protocol
│   └── metadata.py # Backup metadata handling
└── server/
    ├── app.py      # FastAPI application
    ├── storage.py  # SQLite database layer
    ├── tasks.py    # Background task manager
    ├── scheduler.py # Cron scheduler
    ├── retention.py # Backup retention policies
    └── web/        # Web UI templates
```

### Key Components

| Component | Description |
|-----------|-------------|
| `app.py` | Main FastAPI server with all API endpoints |
| `storage.py` | SQLite database operations, schema migrations |
| `proxmox.py` | Proxmox VE API integration for hypervisor backups |
| `tasks.py` | Async background task execution |
| `retention.py` | Backup retention policy enforcement |

### Creating a Release

```bash
# Update version and create tag
make release VERSION=0.2.0

# Push to GitHub (triggers automated build)
git push && git push --tags
```

CI/CD Pipeline:
1. Runs tests on Linux and Windows
2. Builds Windows agent installer (`.exe`)
3. Builds Docker image → `ghcr.io/stocky789/backer`
4. Builds Python packages (`.whl`, `.tar.gz`)
5. Creates GitHub release with all artifacts

### Dev Branch Workflow

```bash
# Create feature branch from dev
git checkout dev
git pull origin dev
git checkout -b feature/my-feature

# Make changes, commit
git add -A
git commit -m "feat: add new feature"

# Push and create PR to dev branch
git push -u origin feature/my-feature
```

### Debugging Tips

```bash
# View server logs
sudo journalctl -u backer -f

# View agent logs
sudo journalctl -u backer-agent -f

# Check SQLite database
sqlite3 ~/.local/share/backer/backer.db ".tables"

# Test Proxmox connectivity
curl -k https://proxmox-host:8006/api2/json/version

# Test SMB share access
smbclient //server/share -U username
```

---

## License

MIT
