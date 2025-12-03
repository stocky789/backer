# Backer

Open-source backup management with web UI - like Veeam/UrBackup but simpler.

**Self-contained**: Backer automatically downloads rclone and restic - no manual tool installation required.

## Quick Start

```bash
# Install
git clone https://github.com/stocky789/backer.git
cd backer
pip install -e ".[all]"

# Setup (downloads backup tools automatically)
backer setup

# Run a backup
backer backup /home/user/documents /mnt/backup/documents
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

## Server + Agent Mode (Recommended)

This is how you'd typically use Backer for multiple machines:

**1. Start the server:**

```bash
backer server start
# Web UI available at http://localhost:8420
```

**2. On each machine to backup:**

```bash
# Install backer
pip install -e ".[client]"

# Register with server
backer agent register --server http://backup-server:8420

# Start agent (keeps running in background)
backer agent start
```

**3. Manage everything from the web UI:**

- See all connected agents
- Create backup jobs
- Run backups on demand
- View backup history
- Monitor status

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

## Installation

### Server (Linux)

```bash
# Option 1: Install from source
git clone https://github.com/stocky789/backer.git
cd backer
pip install -e ".[server]"
backer setup  # Downloads rclone + restic

# Option 2: Install from PyPI (when released)
pip install backer[server]

# Start server
backer server start
# Web UI: http://localhost:8420
```

**Run as a systemd service:**

```bash
# Create service file
cat > /etc/systemd/system/backer.service << 'EOF'
[Unit]
Description=Backer Backup Server
After=network.target

[Service]
Type=simple
User=backer
ExecStart=/usr/local/bin/backer server start --host 0.0.0.0 --port 8420
Restart=always

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
sudo systemctl enable backer
sudo systemctl start backer
```

### Windows Agent

**Option 1: Download pre-built executable**

1. Go to [Releases](https://github.com/stocky789/backer/releases)
2. Download `backer-agent-windows-amd64.zip`
3. Extract and run in Command Prompt:

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

### Linux Agent

```bash
pip install backer[client]
backer agent register --server http://your-server:8420
backer agent install --method systemd
```

## Development

```bash
# Install with dev dependencies
make install-dev

# Run tests
make test

# Quick backup test
make demo

# Lint and format
make lint
make format

# Build Python package
make build

# Build Windows agent (on Windows with PyInstaller)
make build-agent
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

## License

MIT
