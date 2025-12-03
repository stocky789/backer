# Backer

Open-source backup management with web UI - like Veeam/UrBackup but simpler.

**Self-contained**: Backer automatically downloads rclone and restic - no manual tool installation required.

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

### Option 1: One-Line Install (Recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/install.sh | sudo bash
```

This installs:
- Python environment and dependencies
- Backer server with web UI
- Systemd service (auto-starts on boot)
- Backup tools (rclone, restic)

**Access Web UI:** `http://your-server:8420`

### Option 2: Docker

```bash
git clone https://github.com/stocky789/backer.git
cd backer
docker compose up -d
```

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

**Option 1: Download Pre-built Executable (Recommended)**

1. Go to [Releases](https://github.com/stocky789/backer/releases)
2. Download `backer-agent-windows-amd64.zip`
3. Extract and run Command Prompt **as Administrator**:

```cmd
backer-agent.exe register --server http://your-server:8420
backer-agent.exe install
```

The agent will:
- Register with your Backer server
- Install as a Windows service (runs at startup)
- Appear in the Web UI under "Agents"

**Option 2: Install from Python**

```powershell
pip install backer[client]
backer agent register --server http://your-server:8420
backer agent install
```

**Uninstall Windows Agent:**

Use Windows Settings > Apps > Backer Agent, or:

```cmd
backer-agent.exe uninstall
```

### Linux Agent

```bash
# Quick install
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/install.sh | sudo bash -s -- --agent

# Or manual install
pip install backer[client]
backer agent register --server http://your-server:8420
backer agent install --method systemd
```

**Agent Commands:**

```bash
backer agent status       # Check connection status
backer agent start        # Start agent manually
backer agent register     # Register with server
```

---

## Web UI Features

Once the server is running, access `http://your-server:8420` to:

- **Dashboard**: Overview of all backups and agent status
- **Agents**: View connected agents, see online/offline status
- **Storage**: Configure backup destinations (SMB shares, NFS, local paths)
- **Backups**: Create and manage backup jobs with schedules
- **History**: View backup run history and logs
- **Restore**: Restore files from backups (supports subfolder restore)

---

## Backup Backends

| Backend | Status | Best For |
|---------|--------|----------|
| **rclone** | ✅ Recommended | Most use cases. Supports SMB, NFS, S3, and 50+ cloud providers. Fast file sync. |
| **restic** | ✅ Supported | Encrypted, deduplicated backups with versioning. Select specific snapshots to restore. |
| **rsync** | ❌ Not Available | Not supported for agent-based backups. Use rclone instead. |

**Note:** Backup tools are automatically downloaded to agents - no manual installation required.

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
  backer agent register     Register agent with server
  backer agent start        Start agent daemon
  backer agent status       Check connection status
  backer agent install      Install as system service

Jobs:
  backer job list           List all jobs
  backer job create         Create a backup job
  backer job run NAME       Run a job manually
```

---

## API

The server exposes a REST API:

```bash
curl http://localhost:8420/health
curl http://localhost:8420/api/v1/clients
curl http://localhost:8420/api/v1/jobs
curl http://localhost:8420/api/v1/jobs/{name}/restore -X POST -d '{"client_id": "..."}'
```

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

## Development

```bash
# Clone and setup dev environment
git clone https://github.com/stocky789/backer.git
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

### Creating a Release

```bash
# Update version and create tag
make release VERSION=0.2.0

# Push to GitHub (triggers automated build)
git push && git push --tags
```

This will:
1. Build Windows agent executable
2. Build Python packages
3. Create GitHub release with downloads

---

## License

MIT
