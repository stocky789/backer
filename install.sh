#!/bin/bash
# Backer Server Installation Script
# One-command installation for Linux systems
#
# Supported distributions:
#   - Debian, Ubuntu, Linux Mint, Pop!_OS
#   - Fedora
#   - RHEL, CentOS, Rocky Linux, AlmaLinux
#   - Arch Linux, Manjaro
#   - openSUSE
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/install.sh | sudo bash
#   or
#   sudo ./install.sh
#
# Options:
#   --uninstall     Remove backer server
#   --help          Show this help
#
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Installation directory
INSTALL_DIR="${BACKER_INSTALL_DIR:-/opt/backer}"
DATA_DIR="${BACKER_DATA_DIR:-/var/lib/backer}"
SERVICE_USER="${BACKER_USER:-backer}"
UNINSTALL=false

info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
step() { echo -e "${CYAN}==>${NC} $1"; }

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --uninstall)
            UNINSTALL=true
            shift
            ;;
        --help|-h)
            head -20 "$0" | tail -16
            exit 0
            ;;
        *)
            error "Unknown option: $1. Use --help for usage."
            ;;
    esac
done

echo ""
echo "========================================"
echo "       Backer Server Installer"
echo "========================================"
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    error "Please run as root: sudo ./install.sh"
fi

# Uninstall function
uninstall() {
    info "Uninstalling Backer Server..."

    systemctl stop backer 2>/dev/null || true
    systemctl disable backer 2>/dev/null || true
    rm -f /etc/systemd/system/backer.service
    systemctl daemon-reload

    rm -f /usr/local/bin/backer
    rm -rf "$INSTALL_DIR"
    rm -f /etc/sudoers.d/backer-mount

    echo ""
    warn "Data directory preserved at: $DATA_DIR"
    warn "To remove data: sudo rm -rf $DATA_DIR"
    warn "To remove user: sudo userdel $SERVICE_USER"
    echo ""
    success "Backer Server uninstalled"
    exit 0
}

if [[ "$UNINSTALL" == true ]]; then
    uninstall
fi

# Detect OS and install dependencies
install_dependencies() {
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        DISTRO_ID="${ID}"
        DISTRO_VERSION="${VERSION_ID}"
    else
        error "Cannot detect Linux distribution"
    fi

    info "Detected OS: $DISTRO_ID $DISTRO_VERSION"

    case "$DISTRO_ID" in
        debian|ubuntu|linuxmint|pop)
            step "Installing dependencies via apt..."
            apt-get update -qq
            apt-get install -y -qq \
                python3 \
                python3-venv \
                python3-pip \
                git \
                curl \
                ca-certificates \
                cifs-utils \
                smbclient \
                sshpass \
                nfs-common \
                > /dev/null 2>&1
            ;;
        fedora)
            step "Installing dependencies via dnf..."
            dnf install -y -q \
                python3 \
                python3-pip \
                python3-virtualenv \
                git \
                curl \
                ca-certificates \
                cifs-utils \
                samba-client \
                sshpass \
                nfs-utils \
                > /dev/null 2>&1
            ;;
        rhel|centos|rocky|almalinux)
            step "Installing dependencies via dnf/yum..."
            # Try dnf first (RHEL 8+), fall back to yum
            if command -v dnf &>/dev/null; then
                # Enable EPEL for sshpass on RHEL-based distros
                dnf install -y -q epel-release 2>/dev/null || true
                dnf install -y -q \
                    python3 \
                    python3-pip \
                    git \
                    curl \
                    ca-certificates \
                    cifs-utils \
                    samba-client \
                    sshpass \
                    nfs-utils \
                    > /dev/null 2>&1
            else
                yum install -y -q epel-release 2>/dev/null || true
                yum install -y -q \
                    python3 \
                    python3-pip \
                    git \
                    curl \
                    ca-certificates \
                    cifs-utils \
                    samba-client \
                    sshpass \
                    nfs-utils \
                    > /dev/null 2>&1
            fi
            ;;
        arch|manjaro|endeavouros|garuda|cachyos)
            step "Installing dependencies via pacman..."
            # Arch uses 'python' not 'python3', and 'python-pip' not 'python3-pip'
            pacman -Sy --noconfirm --needed \
                python \
                python-pip \
                git \
                curl \
                ca-certificates \
                cifs-utils \
                smbclient \
                sshpass \
                nfs-utils \
                > /dev/null 2>&1
            ;;
        opensuse*|sles)
            step "Installing dependencies via zypper..."
            # Note: python3-virtualenv may not be in default repos, but we use python3 -m venv
            zypper install -y \
                python3 \
                python3-pip \
                git \
                curl \
                ca-certificates \
                cifs-utils \
                samba-client \
                sshpass \
                nfs-client \
                > /dev/null 2>&1 || true
            ;;
        *)
            warn "Unknown distro: $DISTRO_ID"
            warn "Attempting to continue - you may need to manually install:"
            warn "  python3, python3-pip, python3-venv, git, curl"
            warn "  cifs-utils, smbclient, sshpass, nfs-common/nfs-utils"
            echo ""
            # Try to detect package manager
            if command -v apt-get &>/dev/null; then
                apt-get update -qq
                apt-get install -y -qq python3 python3-venv python3-pip git curl cifs-utils smbclient sshpass nfs-common 2>/dev/null || true
            elif command -v dnf &>/dev/null; then
                dnf install -y -q python3 python3-pip git curl cifs-utils samba-client sshpass nfs-utils 2>/dev/null || true
            elif command -v yum &>/dev/null; then
                yum install -y -q python3 python3-pip git curl cifs-utils samba-client sshpass nfs-utils 2>/dev/null || true
            elif command -v pacman &>/dev/null; then
                pacman -Sy --noconfirm --needed python python-pip git curl cifs-utils smbclient sshpass nfs-utils 2>/dev/null || true
            elif command -v zypper &>/dev/null; then
                zypper install -y -q python3 python3-pip git curl cifs-utils samba-client sshpass nfs-client 2>/dev/null || true
            fi
            ;;
    esac

    # Verify critical dependencies
    if ! command -v python3 &>/dev/null; then
        error "python3 is required but not installed"
    fi
    if ! command -v git &>/dev/null; then
        error "git is required but not installed"
    fi

    # Warn about optional dependencies
    if ! command -v sshpass &>/dev/null; then
        warn "sshpass not installed - SSH password authentication for Proxmox cleanup will not work"
        warn "You can still use SSH key authentication or install sshpass manually"
    fi

    if ! command -v smbclient &>/dev/null; then
        warn "smbclient not installed - SMB share directory creation may not work"
    fi

    success "System dependencies installed"
}

# Run dependency installation
install_dependencies

# Create service user
if ! id "$SERVICE_USER" &>/dev/null; then
    info "Creating service user: $SERVICE_USER"
    useradd --system --home-dir "$DATA_DIR" --shell /bin/false "$SERVICE_USER"
    success "User $SERVICE_USER created"
else
    info "User $SERVICE_USER already exists"
fi

# Configure sudoers for NFS mount access
# This allows the backer user to mount/unmount NFS shares without password
SUDOERS_FILE="/etc/sudoers.d/backer-mount"
if [ ! -f "$SUDOERS_FILE" ]; then
    info "Configuring sudo access for NFS mounts..."
    cat > "$SUDOERS_FILE" << EOF
# Allow backer user to mount/unmount filesystems for NFS repository access
$SERVICE_USER ALL=(ALL) NOPASSWD: /usr/bin/mount, /usr/bin/umount
EOF
    chmod 440 "$SUDOERS_FILE"
    success "Sudo access configured for mount/umount"
else
    info "Sudo mount access already configured"
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

# Security hardening (NoNewPrivileges disabled to allow sudo for NFS mounts)
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
