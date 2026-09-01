# Backer

Open-source backup management with a web UI. Supports agent-based backups for Windows/Linux machines and Proxmox VE hypervisor integration.

On first start, Backer opens a setup wizard. Create the administrator account there; no password is stored in an environment file.

## Quick Start

### Docker Compose (fastest)

```bash
git clone https://git.stockhome.com.au/stocky789/backer.git
cd backer
sudo docker compose up -d --build
```

Access the web UI at `http://localhost:8420` (or `http://your-server:8420` on remote VM).

Get the one-time setup token from the startup logs, then open the URL and complete the setup wizard:

```bash
sudo docker compose logs backer | grep 'First-run setup token'
```

For the Linux installer, use `sudo journalctl -u backer -b` and find the same line.
Set `BACKER_SETUP_TOKEN` in the server environment before first start to use an
operator-chosen token. Enter the address agents will use to reach Backer (for example,
`http://192.168.1.100:8420` or `https://backer.example.com`).

### Linux Installer

```bash
curl -fsSL https://git.stockhome.com.au/stocky789/backer/raw/branch/main/install.sh | sudo bash
```

Access the web UI at `http://your-server:8420`.

Complete the setup wizard at the displayed address to create your administrator account.
Find its setup token with `sudo journalctl -u backer -b`; set `BACKER_SETUP_TOKEN`
before first start to choose it yourself.

## Development Status

### Agent Backups
- **Windows**: Supported for backup and restore. SMB targets need administrator permissions and one credential set per server.
- **Linux**: Supported for backup and restore.
- **Metadata discovery**: Supported for adopting existing jobs from storage repositories.

### Hypervisor Backups
- **Proxmox standalone nodes**: Supported for VM and LXC backup and restore.
- **Hyper-V clusters**: Experimental. Backup and restore paths exist, but storage auto-discovery still needs validation.
- **Unraid**: Experimental. Do not treat it as release-ready yet.

### Mobile
- **Android agent**: Experimental and server-relay-only. It has no serverless mode in v1.

## Installation

### Server (Linux)

```bash
# Stable release
curl -fsSL https://git.stockhome.com.au/stocky789/backer/raw/branch/main/install.sh | sudo bash

# Development branch
curl -fsSL https://git.stockhome.com.au/stocky789/backer/raw/branch/dev/install.sh | sudo bash -s -- --branch dev
```

Access the web UI at `http://your-server:8420`

### Docker

Docker downloads Kopia on first use into its persistent data volume, verifying its release checksum. SMB/NFS repositories are mounted inside the container, so the compose/run config must include `SYS_ADMIN` and `apparmor:unconfined`.

**Docker Compose** is the recommended way to run Backer server. See Quick Start above.

#### Docker Run (Alternative)

```bash
docker build -t backer:0.9.0 .
docker run -d --name backer \
  -p 8420:8420 \
  -v backer-data:/data \
  --cap-add SYS_ADMIN \
  --security-opt apparmor:unconfined \
  backer:0.9.0
```

#### Docker Security Notes

- `SYS_ADMIN` capability is required for mounting SMB/NFS shares inside the container
- `apparmor:unconfined` allows the container to perform mount operations
- The `backer` user runs with minimal privileges (non-root)
- Backup tools are downloaded on first use, checksum-verified, and cached in the persistent volume

### Windows Agent

Download `backer-agent-setup.exe` from [Releases](https://git.stockhome.com.au/stocky789/backer/releases/tag/release-main).

The Windows installer is currently unsigned.

### Linux Agent

```bash
# Stable
curl -fsSL https://git.stockhome.com.au/stocky789/backer/raw/branch/main/scripts/install-agent.sh | sudo bash

# Development branch
curl -fsSL https://git.stockhome.com.au/stocky789/backer/raw/branch/dev/scripts/install-agent.sh | sudo bash -s -- --branch dev
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
- Encrypted, compressed, deduplicated Kopia snapshots
- Kopia repositories on local directories, SMB shares, and S3-compatible storage
- Repository-level encryption passwords and S3-compatible provider credentials
- Cron scheduling with retention policies
- Web dashboard with backup history

## Storage Repositories

Serverless v1 supports these tested client-to-repository combinations:

| Repository | Serverless Linux | Serverless Windows | Server-managed mode only |
| --- | --- | --- | --- |
| Local directory | Supported | Supported | |
| SMB/CIFS | Supported | Supported | |
| S3-compatible | Supported | Supported | |
| NFS and other repository types | | | Supported, with no serverless CI coverage |

Kopia supports concurrent-writer repositories. Assign one designated maintenance owner per repository for retention and verification. Backer provides no cross-machine lease.

### SMB/CIFS (Windows Shares)
- Network shares accessible via SMB protocol
- Supports authentication with username/password/domain
- Auto-discovery of available shares
- Connection pooling to avoid Windows Error 1219

### NFS (Linux/Unix, server-managed mode only)
- NFS exports from Linux/Unix NAS devices
- Auto-discovery of available exports
- Requires mount permissions (root or passwordless sudo)

### Local directory (server-managed, via the proxy relay)
Store backups directly on the Backer server filesystem using local paths.

#### Why Local Directory?
- **Docker-friendly**: Avoids SMB/NFS mounts inside containers
- **Agent support**: Works with Windows and Linux agents
- **Simpler permissions**: No network share authentication needed
- **Reverse proxy compatible**: Works with Cloudflare, nginx, Traefik, and similar proxies

#### How It Works
Agents stream backup data to the server via its authenticated local-repository transport. The server creates Kopia snapshots on the configured path.

#### Docker Setup
```yaml
# docker-compose.yml
services:
  backer:
    build: .
    volumes:
      - backer-data:/data
      # Mount your backup destination into the container
      - /mnt/nas/backups:/data/backups        # NAS mount
      # OR
      - ./backups:/data/backups               # Local folder
      # OR
      - /path/to/external:/data/external      # External drive
```

#### Usage
1. In the Backer UI, go to **Repositories** → **Add Repository**
2. Select **Local Directory** as the type
3. Enter the container path: `/data/backups` (matches the volume mount)
4. Give it a name like "NAS Backups"
5. Create backup jobs pointing to this repository

The agents will automatically stream data to the server, which writes it to the mounted path.

#### Reverse Proxy Configuration
If using a reverse proxy (Cloudflare, nginx, etc.), enter its external address in **Settings** → **Public URL** (or in the setup wizard on first start).

Agents will use this URL to connect and stream backup data.

### Local directory (serverless, on this client)

Store backups directly on the client filesystem. Kopia writes to this directory without proxying data through the Backer server. Keep repository maintenance on one designated machine when multiple clients write to the same repository.

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

- [Releases](https://git.stockhome.com.au/stocky789/backer/releases/tag/release-main)
- [Repository](https://git.stockhome.com.au/stocky789/backer)

## License

MIT
