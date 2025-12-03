# Backer

Open-source backup management with web UI - like Veeam/UrBackup but simpler.

**Self-contained**: Backer automatically downloads rclone and restic - no manual tool installation required.

## Quick Start

### Option 1: One-Line Install (Recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/install.sh | sudo bash
```

This installs everything: dependencies, Python environment, backup tools, and starts the service.

**Web UI:** `http://your-server:8420`

### Option 2: Docker

```bash
git clone https://github.com/stocky789/backer.git
cd backer
docker compose up -d
```

**Web UI:** `http://localhost:8420`

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

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Backer Server + Web UI                    │
│    - Manage agents from web dashboard                        │
│    - Configure and schedule backup jobs                      │
│    - Monitor backup status in real-time                      │
│    - View backup history and logs                            │
│    http://localhost:8420                                     │
└─────────────────────────────────────────────────────────────┘
                              │
           ┌──────────────────┼──────────────────┐
           │                  │                  │
           ▼                  ▼                  ▼
    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
    │   Agent     │    │   Agent     │    │   Agent     │
    │ (Workstation)│   │  (Server)   │    │   (NAS)     │
    └─────────────┘    └─────────────┘    └─────────────┘
           │                  │                  │
           ▼                  ▼                  ▼
    ┌───────────────────────────────────────────────────────┐
    │          Backup Tools (auto-downloaded)                │
    │              rclone  •  restic                         │
    └───────────────────────────────────────────────────────┘
```

## Server Management

After installation with the install script:

```bash
# Service commands
sudo systemctl status backer    # Check status
sudo systemctl restart backer   # Restart
sudo systemctl stop backer      # Stop
sudo journalctl -u backer -f    # View logs

# CLI commands (available system-wide)
backer --help
backer tools
```

## Windows Agent

**Option 1: Download pre-built executable**

1. Go to [Releases](https://github.com/stocky789/backer/releases)
2. Download `backer-agent-windows-amd64.zip`
3. Extract and run in Command Prompt as Administrator:

```cmd
backer-agent.exe register --server http://your-server:8420
backer-agent.exe install
```

**Option 2: Install from Python**

```powershell
pip install backer[client]
backer agent register --server http://your-server:8420
backer agent install
```

## Linux Agent

```bash
# Quick install
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/install.sh | sudo bash -s -- --agent

# Or manual
pip install backer[client]
backer agent register --server http://your-server:8420
backer agent install --method systemd
```

## CLI Commands

```
Setup:
  backer setup              Download and install backup tools
  backer tools              Show installed tools status
  backer backends           Show available backends

Backup:
  backer backup SRC DEST    Run a backup
  backer restore SRC DEST   Restore from backup

Server:
  backer server start       Start server with web UI

Agent:
  backer agent register     Register agent with server
  backer agent start        Start agent daemon
  backer agent status       Check connection status

Jobs:
  backer job list           List all jobs
  backer job create         Create a backup job
  backer job run NAME       Run a job manually
```

## Standalone Mode (Single Machine)

For simple single-machine backups, no server needed:

```bash
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

## Creating a Release

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

## API

The server exposes a REST API:

```bash
curl http://localhost:8420/health
curl http://localhost:8420/api/v1/clients
curl http://localhost:8420/api/v1/jobs
```

## System Requirements

**Server:**
- Linux (Debian/Ubuntu recommended)
- Python 3.10+
- 512MB RAM minimum
- SMB support: `cifs-utils`, `smbclient`
- NFS support: `nfs-common`

**Windows Agent:**
- Windows 10/11 or Windows Server 2016+
- No Python required (standalone executable)

## License

MIT
