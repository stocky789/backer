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
#   --branch NAME   Install from specific branch (default: main)
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
GIT_BRANCH="${BACKER_BRANCH:-main}"
UNINSTALL=false

info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
step() { echo -e "${CYAN}==>${NC} $1"; }

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --branch)
            GIT_BRANCH="$2"
            shift 2
            ;;
        --uninstall)
            UNINSTALL=true
            shift
            ;;
        --help|-h)
            head -22 "$0" | tail -18
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
if [[ "$GIT_BRANCH" != "main" ]]; then
echo "         Branch: $GIT_BRANCH"
fi
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
                python3-dev \
                git \
                curl \
                ca-certificates \
                cifs-utils \
                smbclient \
                sshpass \
                nfs-common \
                gcc \
                libkrb5-dev \
                > /dev/null 2>&1
            ;;
        fedora)
            step "Installing dependencies via dnf..."
            dnf install -y -q \
                python3 \
                python3-pip \
                python3-devel \
                git \
                curl \
                ca-certificates \
                cifs-utils \
                samba-client \
                sshpass \
                nfs-utils \
                gcc \
                krb5-devel \
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
                    python3-devel \
                    git \
                    curl \
                    ca-certificates \
                    cifs-utils \
                    samba-client \
                    sshpass \
                    nfs-utils \
                    gcc \
                    krb5-devel \
                    > /dev/null 2>&1
            else
                yum install -y -q epel-release 2>/dev/null || true
                yum install -y -q \
                    python3 \
                    python3-pip \
                    python3-devel \
                    git \
                    curl \
                    ca-certificates \
                    cifs-utils \
                    samba-client \
                    sshpass \
                    nfs-utils \
                    gcc \
                    krb5-devel \
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
                gcc \
                krb5 \
                > /dev/null 2>&1
            ;;
        opensuse*|sles)
            step "Installing dependencies via zypper..."
            # Note: python3-virtualenv may not be in default repos, but we use python3 -m venv
            zypper install -y \
                python3 \
                python3-pip \
                python3-devel \
                git \
                curl \
                ca-certificates \
                cifs-utils \
                samba-client \
                sshpass \
                nfs-client \
                gcc \
                krb5-devel \
                > /dev/null 2>&1 || true
            ;;
        *)
            warn "Unknown distro: $DISTRO_ID"
            warn "Attempting to continue - you may need to manually install:"
            warn "  python3, python3-pip, python3-venv, python3-dev, git, curl, gcc"
            warn "  cifs-utils, smbclient, sshpass, nfs-common/nfs-utils, libkrb5-dev/krb5-devel"
            echo ""
            # Try to detect package manager and install dependencies including build tools
            if command -v apt-get &>/dev/null; then
                apt-get update -qq
                apt-get install -y -qq python3 python3-venv python3-pip python3-dev git curl cifs-utils smbclient sshpass nfs-common gcc libkrb5-dev 2>/dev/null || true
            elif command -v dnf &>/dev/null; then
                dnf install -y -q python3 python3-pip python3-devel git curl cifs-utils samba-client sshpass nfs-utils gcc krb5-devel 2>/dev/null || true
            elif command -v yum &>/dev/null; then
                yum install -y -q python3 python3-pip python3-devel git curl cifs-utils samba-client sshpass nfs-utils gcc krb5-devel 2>/dev/null || true
            elif command -v pacman &>/dev/null; then
                pacman -Sy --noconfirm --needed python python-pip git curl cifs-utils smbclient sshpass nfs-utils gcc krb5 2>/dev/null || true
            elif command -v zypper &>/dev/null; then
                zypper install -y -q python3 python3-pip python3-devel git curl cifs-utils samba-client sshpass nfs-client gcc krb5-devel 2>/dev/null || true
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
    # Switch to the correct branch and pull
    git fetch --quiet origin
    git checkout --quiet "$GIT_BRANCH" 2>/dev/null || git checkout --quiet -b "$GIT_BRANCH" "origin/$GIT_BRANCH"
    git pull --quiet origin "$GIT_BRANCH"
else
    info "Cloning Backer repository (branch: $GIT_BRANCH)..."
    git clone --quiet --branch "$GIT_BRANCH" https://github.com/stocky789/backer.git "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi
success "Source code ready (branch: $GIT_BRANCH)"

# Create virtual environment
info "Setting up Python environment..."
python3 -m venv "$INSTALL_DIR/venv"
source "$INSTALL_DIR/venv/bin/activate"

# Install backer with progress indication
info "Installing Python dependencies (this may take 1-2 minutes)..."
pip install --upgrade pip > /dev/null 2>&1

# Show progress while pip installs (can take a while due to pywinrm/cryptography)
# Use process substitution to capture exit code correctly (avoid PIPESTATUS being overwritten by loop pipelines)
set +e  # Temporarily disable exit on error to capture pip exit code
pip install --no-cache-dir --force-reinstall -e "$INSTALL_DIR[server]" 2>&1 | while read -r line; do
    # Show package names as they're installed
    if [[ "$line" =~ "Successfully installed" ]]; then
        echo -e "  ${GREEN}✓${NC} Dependencies installed"
    elif [[ "$line" =~ "Collecting" ]]; then
        # Use parameter expansion instead of sed pipeline to avoid overwriting PIPESTATUS
        pkg="${line#Collecting }"
        pkg="${pkg%% *}"
        echo -ne "\r  Installing: $pkg                              \r"
    elif [[ "$line" =~ "Building wheel" ]]; then
        echo -ne "\r  Building native extensions...                  \r"
    fi
done
PIP_EXIT=${PIPESTATUS[0]}  # Capture pip exit code immediately after pipeline
set -e  # Re-enable exit on error
echo -ne "\r                                                        \r"
if [ $PIP_EXIT -ne 0 ]; then
    error "Failed to install Backer dependencies. Exit code: $PIP_EXIT"
fi
success "Backer installed"

# Verify installation (basic check that backer is importable)
info "Verifying installation..."
if ! "$INSTALL_DIR/venv/bin/python" -c "import backer; import backer.server" 2>/dev/null; then
    warn "Installation verification failed. Trying to reinstall..."
    # Reinstall with --force-reinstall to ensure packages are properly installed
    "$INSTALL_DIR/venv/bin/pip" install --no-cache-dir --force-reinstall "file://$INSTALL_DIR[server]" > /dev/null 2>&1 || true
fi
success "Installation verified"

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
# Run as root for full filesystem access
# Backup server needs to access:
#   - All user home directories (/home/*)
#   - System paths for backup jobs
#   - Network mount points (SMB, NFS)
#   - Local repository paths
User=root
Group=root
WorkingDirectory=$INSTALL_DIR
Environment="BACKER_DATA_DIR=$DATA_DIR"
ExecStart=$INSTALL_DIR/venv/bin/backer server start --host 0.0.0.0 --port 8420
Restart=always
RestartSec=5

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

# Wait for the HTTP server to be ready (not just the process)
info "Waiting for server to be ready..."
MAX_WAIT=60
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
    if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8420/health 2>/dev/null | grep -q "200"; then
        break
    fi
    sleep 2
    WAITED=$((WAITED + 2))
    # Show progress every 10 seconds
    if [ $((WAITED % 10)) -eq 0 ] && [ $WAITED -gt 0 ]; then
        echo -ne "\r${CYAN}==>${NC} Still starting... (${WAITED}s)"
    fi
done
echo -ne "\r"  # Clear the progress line

# Check if running
if curl -s -f -o /dev/null http://127.0.0.1:8420/health 2>/dev/null; then
    success "Backer service is running and ready"
elif systemctl is-active --quiet backer; then
    warn "Service is running but API not responding yet. It may need more time to initialize."
    warn "Check: journalctl -u backer -f"
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
