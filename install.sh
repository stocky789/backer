#!/bin/bash
# Backer Server Installation Script
# One-command installation for Debian/Ubuntu systems
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/install.sh | bash
#   or
#   ./install.sh
#
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Installation directory
INSTALL_DIR="${BACKER_INSTALL_DIR:-/opt/backer}"
DATA_DIR="${BACKER_DATA_DIR:-/var/lib/backer}"
SERVICE_USER="${BACKER_USER:-backer}"

info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

echo ""
echo "========================================"
echo "       Backer Server Installer"
echo "========================================"
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    error "Please run as root: sudo ./install.sh"
fi

# Detect OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
    VERSION=$VERSION_ID
else
    error "Cannot detect OS. This script supports Debian/Ubuntu."
fi

info "Detected OS: $OS $VERSION"

# Check for supported OS
case $OS in
    ubuntu|debian)
        PKG_MANAGER="apt-get"
        ;;
    *)
        error "Unsupported OS: $OS. This script supports Debian/Ubuntu."
        ;;
esac

# Install system dependencies
info "Installing system dependencies..."
$PKG_MANAGER update -qq

# Core Python
$PKG_MANAGER install -y -qq \
    python3 \
    python3-venv \
    python3-pip \
    git \
    curl \
    > /dev/null 2>&1

# SMB/CIFS support for Windows shares
$PKG_MANAGER install -y -qq \
    cifs-utils \
    smbclient \
    > /dev/null 2>&1

# SSH utilities for incremental backups (QMP over SSH)
$PKG_MANAGER install -y -qq \
    sshpass \
    > /dev/null 2>&1

# NFS support
$PKG_MANAGER install -y -qq \
    nfs-common \
    > /dev/null 2>&1

success "System dependencies installed"

# Create service user
if ! id "$SERVICE_USER" &>/dev/null; then
    info "Creating service user: $SERVICE_USER"
    useradd --system --home-dir "$DATA_DIR" --shell /bin/false "$SERVICE_USER"
    success "User $SERVICE_USER created"
else
    info "User $SERVICE_USER already exists"
fi

# Create directories
info "Creating directories..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$DATA_DIR"
mkdir -p "$DATA_DIR/tools"
mkdir -p "$DATA_DIR/logs"

# Clone or update repository
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Updating existing installation..."
    cd "$INSTALL_DIR"
    git pull --quiet
else
    info "Cloning Backer repository..."
    git clone --quiet https://github.com/stocky789/backer.git "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi
success "Source code ready"

# Create virtual environment
info "Setting up Python environment..."
python3 -m venv "$INSTALL_DIR/venv"
source "$INSTALL_DIR/venv/bin/activate"

# Install backer
pip install --quiet --upgrade pip
pip install --quiet --no-cache-dir --force-reinstall -e "$INSTALL_DIR[server]"
success "Backer installed"

# Download backup tools (rclone + restic)
info "Downloading backup tools..."
"$INSTALL_DIR/venv/bin/backer" setup --data-dir "$DATA_DIR" 2>/dev/null || \
    "$INSTALL_DIR/venv/bin/backer" setup 2>/dev/null || true
success "Backup tools ready"

# Set permissions
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# Create systemd service
info "Creating systemd service..."
cat > /etc/systemd/system/backer.service << EOF
[Unit]
Description=Backer Backup Server
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment="BACKER_DATA_DIR=$DATA_DIR"
ExecStart=$INSTALL_DIR/venv/bin/backer server start --host 0.0.0.0 --port 8420
Restart=always
RestartSec=5

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA_DIR
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
success "Systemd service created"

# Create convenience wrapper
info "Creating backer command..."
cat > /usr/local/bin/backer << EOF
#!/bin/bash
# Backer wrapper - runs backer from the installed venv
export BACKER_DATA_DIR="$DATA_DIR"
exec "$INSTALL_DIR/venv/bin/backer" "\$@"
EOF
chmod +x /usr/local/bin/backer
success "Command 'backer' available system-wide"

# Enable and start service
info "Starting Backer service..."
systemctl enable backer --quiet
systemctl start backer

# Wait for startup
sleep 2

# Check if running
if systemctl is-active --quiet backer; then
    success "Backer service is running"
else
    warn "Service may not have started correctly. Check: journalctl -u backer"
fi

echo ""
echo "========================================"
echo "       Installation Complete!"
echo "========================================"
echo ""
echo "  Web UI:  http://$(hostname -I | awk '{print $1}'):8420"
echo ""
echo "  Commands:"
echo "    sudo systemctl status backer   # Check status"
echo "    sudo systemctl restart backer  # Restart"
echo "    sudo journalctl -u backer -f   # View logs"
echo "    backer --help                  # CLI help"
echo ""
echo "  Data directory: $DATA_DIR"
echo "  Install directory: $INSTALL_DIR"
echo ""
