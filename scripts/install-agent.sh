#!/usr/bin/env bash
#
# Backer Agent Installer for Linux
#
# Installs the Backer backup agent system-wide.
# Requires: sudo/root access
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/scripts/install-agent.sh | sudo bash
#
# Options:
#   --uninstall     Remove backer-agent
#   --version VER   Install specific version (default: latest)
#   --help          Show this help
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# Fixed paths - always system-wide
INSTALL_DIR="/opt/backer"
CONFIG_DIR="/etc/backer"
BACKER_VERSION="latest"
UNINSTALL=false

# Print functions
info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
step() { echo -e "${CYAN}==>${NC} $1"; }

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --version)
            BACKER_VERSION="$2"
            shift 2
            ;;
        --uninstall)
            UNINSTALL=true
            shift
            ;;
        --help|-h)
            head -18 "$0" | tail -14
            exit 0
            ;;
        *)
            error "Unknown option: $1. Use --help for usage."
            ;;
    esac
done

# Must be root
check_root() {
    if [[ $EUID -ne 0 ]]; then
        error "This installer requires root. Run with: sudo bash install-agent.sh"
    fi
}

# Detect distro and setup package manager
setup_packages() {
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        DISTRO_ID="${ID}"
    else
        error "Cannot detect Linux distribution"
    fi

    case "$DISTRO_ID" in
        debian|ubuntu|linuxmint|pop)
            apt-get update -qq
            apt-get install -y -qq python3 python3-pip python3-venv curl ca-certificates cifs-utils nfs-common
            ;;
        fedora)
            dnf install -y -q python3 python3-pip curl ca-certificates cifs-utils nfs-utils
            ;;
        rhel|centos|rocky|almalinux)
            dnf install -y -q python3 python3-pip curl ca-certificates cifs-utils nfs-utils || \
            yum install -y -q python3 python3-pip curl ca-certificates cifs-utils nfs-utils
            ;;
        arch|manjaro)
            pacman -Sy --noconfirm --needed python python-pip curl ca-certificates cifs-utils nfs-utils
            ;;
        opensuse*)
            zypper install -y -q python3 python3-pip curl ca-certificates cifs-utils nfs-client
            ;;
        *)
            warn "Unknown distro: $DISTRO_ID - assuming python3 is available"
            warn "You may need to manually install: cifs-utils nfs-common"
            ;;
    esac
}

# Install backer
install_backer() {
    step "Creating directories..."
    mkdir -p "$INSTALL_DIR"
    mkdir -p "$CONFIG_DIR"

    step "Setting up Python environment..."
    python3 -m venv "$INSTALL_DIR/venv"
    source "$INSTALL_DIR/venv/bin/activate"
    pip install --upgrade pip wheel setuptools -q

    step "Installing backer from GitHub..."
    if [[ "$BACKER_VERSION" == "latest" ]]; then
        pip install --no-cache-dir --force-reinstall "backer[client] @ git+https://github.com/stocky789/backer.git" || \
            error "Failed to install backer"
    else
        pip install --no-cache-dir --force-reinstall "backer[client] @ git+https://github.com/stocky789/backer.git@v$BACKER_VERSION" || \
            error "Failed to install backer version $BACKER_VERSION"
    fi

    deactivate

    # Symlink for easy access
    ln -sf "$INSTALL_DIR/venv/bin/backer" /usr/local/bin/backer

    success "Backer installed to $INSTALL_DIR"
}

# Install backup tools (rclone, restic, kopia)
install_tools() {
    step "Downloading backup tools (rclone, restic, kopia)..."

    # Run backer setup to download the tools
    # The tools are installed to ~/.local/share/backer/tools/ for root user
    "$INSTALL_DIR/venv/bin/backer" setup || warn "Some tools may not have installed correctly"

    success "Backup tools installed"
}

# Create systemd service
create_service() {
    step "Creating systemd service..."

    cat > /etc/systemd/system/backer-agent.service << 'EOF'
[Unit]
Description=Backer Backup Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/opt/backer/venv/bin/backer agent start
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=backer-agent

# Config location
Environment="BACKER_CONFIG_DIR=/etc/backer"

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    success "Service created"
}

# Run setup wizard
run_wizard() {
    # Check if interactive
    if [[ ! -t 0 ]]; then
        echo
        warn "Non-interactive install detected."
        info "Run setup manually: sudo backer agent setup"
        return
    fi

    echo
    step "Running setup wizard..."
    echo

    # Set config dir for wizard
    export BACKER_CONFIG_DIR="$CONFIG_DIR"
    "$INSTALL_DIR/venv/bin/backer" agent setup

    # Start service if config exists
    if [[ -f "$CONFIG_DIR/agent.yaml" ]]; then
        step "Starting agent service..."
        systemctl enable backer-agent
        systemctl start backer-agent
        success "Agent is running!"
    fi
}

# Uninstall
uninstall() {
    info "Uninstalling Backer Agent..."

    systemctl stop backer-agent 2>/dev/null || true
    systemctl disable backer-agent 2>/dev/null || true
    rm -f /etc/systemd/system/backer-agent.service
    systemctl daemon-reload

    rm -f /usr/local/bin/backer
    rm -rf "$INSTALL_DIR"

    # Always remove config on uninstall - old config causes auth issues on reinstall
    if [[ -d "$CONFIG_DIR" ]]; then
        info "Removing configuration in $CONFIG_DIR"
        rm -rf "$CONFIG_DIR"
    fi

    success "Backer Agent uninstalled"
}

# Print final instructions
print_done() {
    echo
    echo -e "${GREEN}════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Backer Agent Installed!${NC}"
    echo -e "${GREEN}════════════════════════════════════════${NC}"
    echo
    echo "Commands:"
    echo "  backer agent status    - Check status"
    echo "  backer agent logs      - View logs"
    echo "  backer agent setup     - Re-run setup wizard"
    echo
    echo "Service:"
    echo "  sudo systemctl status backer-agent"
    echo "  sudo systemctl restart backer-agent"
    echo
}

# Main
main() {
    echo
    echo -e "${BLUE}╔═══════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║       Backer Agent Installer          ║${NC}"
    echo -e "${BLUE}╚═══════════════════════════════════════╝${NC}"
    echo

    check_root

    if [[ "$UNINSTALL" == true ]]; then
        uninstall
        exit 0
    fi

    setup_packages
    install_backer
    install_tools
    create_service
    run_wizard
    print_done
}

main "$@"
