#!/usr/bin/env bash
#
# Backer Agent Installer for Linux
# Supports: Debian/Ubuntu, Fedora/RHEL/CentOS, Arch, openSUSE, Alpine, NixOS
#
# Usage:
#   curl -sSL https://get.backer.io/agent | bash
#   or
#   ./install-agent.sh [OPTIONS]
#
# Options:
#   --server URL    Server URL to register with after install
#   --uninstall     Remove backer-agent
#   --version VER   Install specific version (default: latest)
#   --no-service    Don't install systemd service
#   --help          Show this help
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Defaults
INSTALL_DIR="/opt/backer"
CONFIG_DIR="/etc/backer"
LOG_DIR="/var/log/backer"
SERVICE_USER="backer"
VERSION="latest"
SERVER_URL=""
NO_SERVICE=false
UNINSTALL=false

# Print functions
info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --server)
            SERVER_URL="$2"
            shift 2
            ;;
        --version)
            VERSION="$2"
            shift 2
            ;;
        --no-service)
            NO_SERVICE=true
            shift
            ;;
        --uninstall)
            UNINSTALL=true
            shift
            ;;
        --help|-h)
            head -25 "$0" | tail -20
            exit 0
            ;;
        *)
            error "Unknown option: $1"
            ;;
    esac
done

# Check if running as root
check_root() {
    if [[ $EUID -ne 0 ]]; then
        error "This script must be run as root (use sudo)"
    fi
}

# Detect Linux distribution
detect_distro() {
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        DISTRO_ID="${ID}"
        DISTRO_ID_LIKE="${ID_LIKE:-}"
        DISTRO_VERSION="${VERSION_ID:-}"
        DISTRO_NAME="${PRETTY_NAME:-$ID}"
    elif [[ -f /etc/debian_version ]]; then
        DISTRO_ID="debian"
        DISTRO_NAME="Debian $(cat /etc/debian_version)"
    elif [[ -f /etc/redhat-release ]]; then
        DISTRO_ID="rhel"
        DISTRO_NAME=$(cat /etc/redhat-release)
    else
        error "Unable to detect Linux distribution"
    fi

    info "Detected: $DISTRO_NAME"
}

# Get package manager and install commands based on distro
setup_package_manager() {
    case "$DISTRO_ID" in
        debian|ubuntu|linuxmint|pop|elementary|zorin)
            PKG_MANAGER="apt"
            PKG_UPDATE="apt-get update -qq"
            PKG_INSTALL="apt-get install -y -qq"
            PYTHON_PKG="python3"
            PIP_PKG="python3-pip"
            VENV_PKG="python3-venv"
            DEPS="curl ca-certificates"
            ;;
        fedora)
            PKG_MANAGER="dnf"
            PKG_UPDATE="dnf check-update || true"
            PKG_INSTALL="dnf install -y -q"
            PYTHON_PKG="python3"
            PIP_PKG="python3-pip"
            VENV_PKG="python3-virtualenv"
            DEPS="curl ca-certificates"
            ;;
        rhel|centos|rocky|almalinux|ol)
            PKG_MANAGER="dnf"
            # Check if dnf exists, fall back to yum
            if ! command -v dnf &>/dev/null; then
                PKG_MANAGER="yum"
                PKG_INSTALL="yum install -y -q"
                PKG_UPDATE="yum check-update || true"
            else
                PKG_UPDATE="dnf check-update || true"
                PKG_INSTALL="dnf install -y -q"
            fi
            PYTHON_PKG="python3"
            PIP_PKG="python3-pip"
            VENV_PKG="python3-virtualenv"
            DEPS="curl ca-certificates"
            # Enable EPEL for older RHEL/CentOS
            if [[ "$DISTRO_ID" == "centos" ]] || [[ "$DISTRO_ID" == "rhel" ]]; then
                $PKG_INSTALL epel-release 2>/dev/null || true
            fi
            ;;
        arch|manjaro|endeavouros)
            PKG_MANAGER="pacman"
            PKG_UPDATE="pacman -Sy --noconfirm"
            PKG_INSTALL="pacman -S --noconfirm --needed"
            PYTHON_PKG="python"
            PIP_PKG="python-pip"
            VENV_PKG=""  # Included in python
            DEPS="curl ca-certificates"
            ;;
        opensuse*|sles)
            PKG_MANAGER="zypper"
            PKG_UPDATE="zypper refresh -q"
            PKG_INSTALL="zypper install -y -q"
            PYTHON_PKG="python3"
            PIP_PKG="python3-pip"
            VENV_PKG="python3-virtualenv"
            DEPS="curl ca-certificates"
            ;;
        alpine)
            PKG_MANAGER="apk"
            PKG_UPDATE="apk update"
            PKG_INSTALL="apk add --no-cache"
            PYTHON_PKG="python3"
            PIP_PKG="py3-pip"
            VENV_PKG="python3-dev"
            DEPS="curl ca-certificates"
            ;;
        nixos)
            PKG_MANAGER="nix"
            # NixOS is special - we'll use nix-env or recommend nix profile
            warn "NixOS detected - using pip install in user environment"
            warn "For production, consider creating a NixOS module"
            PYTHON_PKG=""
            PIP_PKG=""
            VENV_PKG=""
            DEPS=""
            ;;
        void)
            PKG_MANAGER="xbps"
            PKG_UPDATE="xbps-install -S"
            PKG_INSTALL="xbps-install -y"
            PYTHON_PKG="python3"
            PIP_PKG="python3-pip"
            VENV_PKG="python3-virtualenv"
            DEPS="curl ca-certificates"
            ;;
        gentoo)
            PKG_MANAGER="emerge"
            PKG_UPDATE="emerge --sync"
            PKG_INSTALL="emerge --quiet"
            PYTHON_PKG="dev-lang/python"
            PIP_PKG="dev-python/pip"
            VENV_PKG=""
            DEPS="net-misc/curl"
            ;;
        *)
            # Check for common package managers
            if command -v apt-get &>/dev/null; then
                setup_apt_fallback
            elif command -v dnf &>/dev/null; then
                setup_dnf_fallback
            elif command -v pacman &>/dev/null; then
                setup_pacman_fallback
            else
                error "Unsupported distribution: $DISTRO_ID. Please install manually."
            fi
            ;;
    esac

    info "Package manager: $PKG_MANAGER"
}

setup_apt_fallback() {
    PKG_MANAGER="apt"
    PKG_UPDATE="apt-get update -qq"
    PKG_INSTALL="apt-get install -y -qq"
    PYTHON_PKG="python3"
    PIP_PKG="python3-pip"
    VENV_PKG="python3-venv"
    DEPS="curl ca-certificates"
}

setup_dnf_fallback() {
    PKG_MANAGER="dnf"
    PKG_UPDATE="dnf check-update || true"
    PKG_INSTALL="dnf install -y -q"
    PYTHON_PKG="python3"
    PIP_PKG="python3-pip"
    VENV_PKG="python3-virtualenv"
    DEPS="curl ca-certificates"
}

setup_pacman_fallback() {
    PKG_MANAGER="pacman"
    PKG_UPDATE="pacman -Sy --noconfirm"
    PKG_INSTALL="pacman -S --noconfirm --needed"
    PYTHON_PKG="python"
    PIP_PKG="python-pip"
    VENV_PKG=""
    DEPS="curl ca-certificates"
}

# Install system dependencies
install_dependencies() {
    info "Installing system dependencies..."

    if [[ "$PKG_MANAGER" == "nix" ]]; then
        # NixOS - check if python3 and pip are available
        if ! command -v python3 &>/dev/null; then
            warn "Python3 not found. On NixOS, add python3 to your configuration.nix"
            warn "Or run: nix-env -iA nixpkgs.python3"
        fi
        return
    fi

    # Update package lists
    info "Updating package lists..."
    $PKG_UPDATE 2>/dev/null || true

    # Install dependencies
    local packages="$PYTHON_PKG $PIP_PKG $DEPS"
    [[ -n "$VENV_PKG" ]] && packages="$packages $VENV_PKG"

    info "Installing: $packages"
    $PKG_INSTALL $packages

    success "Dependencies installed"
}

# Check Python version
check_python() {
    if ! command -v python3 &>/dev/null; then
        error "Python3 not found. Please install Python 3.10 or later."
    fi

    PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
    PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

    if [[ "$PYTHON_MAJOR" -lt 3 ]] || [[ "$PYTHON_MAJOR" -eq 3 && "$PYTHON_MINOR" -lt 10 ]]; then
        error "Python 3.10 or later required. Found: Python $PYTHON_VERSION"
    fi

    success "Python $PYTHON_VERSION found"
}

# Create backer user
create_user() {
    if id "$SERVICE_USER" &>/dev/null; then
        info "User '$SERVICE_USER' already exists"
        return
    fi

    info "Creating system user '$SERVICE_USER'..."

    case "$DISTRO_ID" in
        alpine)
            adduser -D -S -s /sbin/nologin -h "$INSTALL_DIR" "$SERVICE_USER"
            ;;
        *)
            useradd --system --shell /sbin/nologin --home-dir "$INSTALL_DIR" \
                    --create-home "$SERVICE_USER" 2>/dev/null || true
            ;;
    esac

    success "User '$SERVICE_USER' created"
}

# Create directories
create_directories() {
    info "Creating directories..."

    mkdir -p "$INSTALL_DIR"
    mkdir -p "$CONFIG_DIR"
    mkdir -p "$LOG_DIR"

    chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
    chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR"
    chown "$SERVICE_USER:$SERVICE_USER" "$LOG_DIR"

    success "Directories created"
}

# Install backer-agent using pip
install_backer() {
    info "Installing backer-agent..."

    # Create virtual environment
    python3 -m venv "$INSTALL_DIR/venv"

    # Activate and install
    source "$INSTALL_DIR/venv/bin/activate"

    # Upgrade pip
    pip install --upgrade pip wheel setuptools -q

    # Install backer with client dependencies
    if [[ "$VERSION" == "latest" ]]; then
        pip install "backer[client]" -q 2>/dev/null || \
        pip install git+https://github.com/stocky789/backer.git#egg=backer[client] -q
    else
        pip install "backer[client]==$VERSION" -q 2>/dev/null || \
        pip install "git+https://github.com/stocky789/backer.git@v$VERSION#egg=backer[client]" -q
    fi

    deactivate

    # Create symlink for easy access
    ln -sf "$INSTALL_DIR/venv/bin/backer" /usr/local/bin/backer

    # Set ownership
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

    success "Backer installed to $INSTALL_DIR"
}

# Download and install backup tools (rclone, restic)
install_backup_tools() {
    info "Setting up backup tools..."

    # Run backer setup as the service user
    sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/backer" setup 2>/dev/null || {
        warn "Auto-setup of backup tools failed. Run 'backer setup' manually."
    }
}

# Create systemd service
create_systemd_service() {
    if [[ "$NO_SERVICE" == true ]]; then
        info "Skipping systemd service (--no-service)"
        return
    fi

    # Check if systemd is available
    if ! command -v systemctl &>/dev/null; then
        warn "Systemd not found - skipping service installation"
        warn "You'll need to start the agent manually or create an init script"
        return
    fi

    info "Creating systemd service..."

    cat > /etc/systemd/system/backer-agent.service << 'EOF'
[Unit]
Description=Backer Backup Agent
Documentation=https://github.com/stocky789/backer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=backer
Group=backer
ExecStart=/opt/backer/venv/bin/backer agent start
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=backer-agent

# Security hardening
NoNewPrivileges=false
ProtectSystem=full
ProtectHome=read-only
PrivateTmp=true
ReadWritePaths=/var/log/backer /etc/backer /opt/backer

# Environment
Environment="HOME=/opt/backer"

[Install]
WantedBy=multi-user.target
EOF

    # Reload systemd
    systemctl daemon-reload

    success "Systemd service created: backer-agent.service"
}

# Create OpenRC service (for Alpine and others)
create_openrc_service() {
    if [[ ! -d /etc/init.d ]] || command -v systemctl &>/dev/null; then
        return
    fi

    info "Creating OpenRC service..."

    cat > /etc/init.d/backer-agent << 'EOF'
#!/sbin/openrc-run

name="backer-agent"
description="Backer Backup Agent"
command="/opt/backer/venv/bin/backer"
command_args="agent start"
command_user="backer"
command_background=true
pidfile="/run/${RC_SVCNAME}.pid"
output_log="/var/log/backer/agent.log"
error_log="/var/log/backer/agent.log"

depend() {
    need net
    after firewall
}
EOF

    chmod +x /etc/init.d/backer-agent
    success "OpenRC service created"
}

# Register with server if URL provided
register_agent() {
    if [[ -z "$SERVER_URL" ]]; then
        return
    fi

    info "Registering agent with server: $SERVER_URL"

    sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/backer" agent register \
        --server "$SERVER_URL" && success "Agent registered" || \
        warn "Registration failed. Register manually with: backer agent register -s $SERVER_URL"
}

# Enable and start service
enable_service() {
    if [[ "$NO_SERVICE" == true ]]; then
        return
    fi

    if command -v systemctl &>/dev/null; then
        info "Enabling and starting backer-agent service..."
        systemctl enable backer-agent
        systemctl start backer-agent
        success "Service started"
    elif [[ -f /etc/init.d/backer-agent ]]; then
        rc-update add backer-agent default
        rc-service backer-agent start
        success "Service started"
    fi
}

# Uninstall function
uninstall() {
    check_root
    info "Uninstalling Backer Agent..."

    # Stop service
    if command -v systemctl &>/dev/null; then
        systemctl stop backer-agent 2>/dev/null || true
        systemctl disable backer-agent 2>/dev/null || true
        rm -f /etc/systemd/system/backer-agent.service
        systemctl daemon-reload
    fi

    if [[ -f /etc/init.d/backer-agent ]]; then
        rc-service backer-agent stop 2>/dev/null || true
        rc-update del backer-agent 2>/dev/null || true
        rm -f /etc/init.d/backer-agent
    fi

    # Remove symlink
    rm -f /usr/local/bin/backer

    # Remove directories (prompt for config)
    rm -rf "$INSTALL_DIR"
    rm -rf "$LOG_DIR"

    read -p "Remove configuration in $CONFIG_DIR? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -rf "$CONFIG_DIR"
    fi

    # Remove user
    userdel "$SERVICE_USER" 2>/dev/null || true

    success "Backer Agent uninstalled"
}

# Print post-install instructions
print_instructions() {
    echo
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}  Backer Agent Installed Successfully!${NC}"
    echo -e "${GREEN}========================================${NC}"
    echo
    echo "Installation directory: $INSTALL_DIR"
    echo "Configuration: $CONFIG_DIR"
    echo "Logs: $LOG_DIR"
    echo
    echo -e "${BLUE}Next steps:${NC}"

    if [[ -z "$SERVER_URL" ]]; then
        echo "  1. Register with your Backer server:"
        echo "     sudo -u backer backer agent register -s http://YOUR-SERVER:8420"
        echo
    fi

    if command -v systemctl &>/dev/null && [[ "$NO_SERVICE" != true ]]; then
        echo "  2. Check service status:"
        echo "     systemctl status backer-agent"
        echo
        echo "  3. View logs:"
        echo "     journalctl -u backer-agent -f"
    else
        echo "  2. Start the agent manually:"
        echo "     sudo -u backer backer agent start"
    fi

    echo
    echo -e "${BLUE}Useful commands:${NC}"
    echo "  backer agent status    - Show agent status"
    echo "  backer tools           - List installed backup tools"
    echo "  backer setup           - Download/update backup tools"
    echo
}

# Main installation flow
main() {
    echo
    echo -e "${BLUE}╔═══════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║     Backer Agent Installer v1.0       ║${NC}"
    echo -e "${BLUE}╚═══════════════════════════════════════╝${NC}"
    echo

    if [[ "$UNINSTALL" == true ]]; then
        uninstall
        exit 0
    fi

    check_root
    detect_distro
    setup_package_manager
    install_dependencies
    check_python
    create_user
    create_directories
    install_backer
    install_backup_tools
    create_systemd_service
    create_openrc_service
    register_agent
    enable_service
    print_instructions
}

# Run main
main "$@"
