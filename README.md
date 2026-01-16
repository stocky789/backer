# Backer

Open-source backup management with a web UI. Supports agent-based backups for Windows/Linux machines and Proxmox VE hypervisor integration.

**Default Login**: `admin` / `admin`

## Quick Start

### Docker (fastest)

```bash
docker run -d --name backer \
  -p 8420:8420 \
  -v backer-data:/data \
  --cap-add SYS_ADMIN \
  --security-opt apparmor:unconfined \
  ghcr.io/stocky789/backer:latest
```

### Linux Installer

```bash
curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/install.sh | sudo bash
```

Access the web UI at `http://your-server:8420`.

## Development Status

### Agent Backups
- **Windows**: ✅ Operational (backup & restore)
- **Linux**: ✅ Operational (backup & restore)
- **Metadata Discovery**: ✅ Can adopt existing jobs from storage repositories

### Hypervisor Backups
- **Proxmox Standalone Nodes**: ✅ Fully functional (backup & restore)
- **Hyper-V Clusters**: ⚠️ Operational (backup & restore), auto-discovery from existing storage repos requires more testing
- **Unraid**: 🚧 Next implementation

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

The Docker image includes pre-installed backup tools (rclone, restic, kopia) for immediate use. SMB/NFS repositories are mounted inside the container, so the compose/run config must include `SYS_ADMIN` and `apparmor:unconfined`.

```bash
docker run -d --name backer \
  -p 8420:8420 \
  -v backer-data:/data \
  --cap-add SYS_ADMIN \
  --security-opt apparmor:unconfined \
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

## Windows Agent - SMB/Network Share Requirements

### Prerequisites
Windows agents backing up to SMB shares require:
- **Administrator privileges** - Required for `net use` and credential management
- **Network access** - Firewall must allow SMB (port 445) to target server
- **Valid credentials** - Username, password, and domain (if applicable)

### Known Limitations

**Windows Error 1219**: Windows only allows **one set of credentials per server**, regardless of the number of shares.

**Examples:**
- ✅ **Works**: `\\server\backup1` and `\\server\backup2` using the **same** credentials
- ❌ **Fails**: `\\server\backup1` (User A) and `\\server\backup2` (User B)

### Solutions

If you encounter Error 1219:

1. **Use the same credentials** for all shares on a server
2. **Disconnect existing connections** before backups:
   ```cmd
   net use \\server\share /delete
   ```
3. **Run agent as service account** - Create a dedicated Windows service account with no interactive logons
4. **Use connection pooling** (automatic) - The agent reuses existing connections when credentials match

### Automatic Retry

The agent automatically retries failed backups up to 3 times with exponential backoff for:
- Network timeouts
- SMB connection errors (including Error 1219)
- Temporary connectivity issues

First retry after 1 second, second after 2 seconds, third after 4 seconds.

### Monitoring

Check SMB connection status in agent logs:
```bash
# Windows
backer agent logs -f

# Look for [SMB-POOL] entries showing connection pool activity
```

Connection pool benefits:
- Prevents Error 1219 by reusing connections
- Reduces connection overhead
- Automatic cleanup on agent shutdown
- Better error messages for credential conflicts

## Links

- [Releases](https://github.com/stocky789/backer/releases)
- [Issues](https://github.com/stocky789/backer/issues)
- [Development Branch](https://github.com/stocky789/backer/tree/dev)

## License

MIT
