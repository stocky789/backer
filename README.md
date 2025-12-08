# Backer

Open-source backup management with a web UI. Supports agent-based backups for Windows/Linux machines and Proxmox VE hypervisor integration.

**Default Login**: `admin` / `admin`

## Installation

### Server (Linux)

```bash
# Stable release
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/install.sh | sudo bash

# Development branch (latest features)
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/install.sh | sudo bash -s -- --branch dev
```

Access the web UI at `http://your-server:8420`

### Docker

```bash
docker run -d --name backer \
  -p 8420:8420 \
  -v backer-data:/data \
  --cap-add SYS_ADMIN \
  ghcr.io/stocky789/backer:latest
```

### Windows Agent

Download `backer-agent-setup.exe` from [Releases](https://github.com/stocky789/backer/releases).

### Linux Agent

```bash
# Stable
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/scripts/install-agent.sh | sudo bash

# Development branch
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/scripts/install-agent.sh | sudo bash -s -- --branch dev
```

## CLI Usage

```bash
# Server
backer server start
backer server uninstall

# Agent
backer agent setup
backer agent status
backer agent logs -f

# Standalone backup (no server)
backer setup
backer backup /source /destination
backer restore /backup /destination
```

## Features

- Agent-based backups for Windows and Linux
- Proxmox VE hypervisor backup (VMs and LXC containers)
- Multiple backends: rclone, restic, kopia
- Storage: SMB, NFS, local, S3
- Cron scheduling with retention policies
- Web dashboard with backup history

## Links

- [Releases](https://github.com/stocky789/backer/releases)
- [Issues](https://github.com/stocky789/backer/issues)
- [Development Branch](https://github.com/stocky789/backer/tree/dev)

## License

MIT
