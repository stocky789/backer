#!/usr/bin/env bash
#
# Backer Agent Installer for Linux
# Supports: Debian/Ubuntu, Fedora/RHEL/CentOS, Arch, openSUSE, Alpine, NixOS
#
# Default: Installs for current user (can backup personal files)
# With --system: Installs system-wide with root access (can backup all files)
#
# Usage:
#   curl -sSL https://get.backer.io/agent | bash
#   or
#   ./install-agent.sh [OPTIONS]
#
# Options:
#   --system        Install system-wide (requires root, can backup all files)
#   --uninstall     Remove backer-agent
#   --version VER   Install specific version (default: latest)
#   --no-wizard     Skip interactive setup wizard
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

# Defaults
SYSTEM_MODE=false
VERSION="latest"
NO_WIZARD=false
UNINSTALL=false
CURRENT_USER=$(whoami)
USER_HOME="$HOME"

# Print functions
info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
step() { echo -e "${CYAN}==>${NC} $1"; }

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --system)
            SYSTEM_MODE=true
            shift
            ;;
        --version)
            VERSION="$2"
            shift 2
            ;;
        --no-wizard)
            NO_WIZARD=true
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

# Set paths based on mode
set_paths() {
    if [[ "$SYSTEM_MODE" == true ]]; then
        # System-wide installation (requires root)
        INSTALL_DIR="/opt/backer"
        CONFIG_DIR="/etc/backer"
        LOG_DIR="/var/log/backer"
        SERVICE_TYPE="system"
    else
        # User installation (personal files)
        INSTALL_DIR="$USER_HOME/.local/share/backer"
        CONFIG_DIR="$USER_HOME/.config/backer"
        LOG_DIR="$USER_HOME/.local/share/backer/logs"
        SERVICE_TYPE="user"
    fi
}

# Check requirements
check_requirements() {
    if [[ "$SYSTEM_MODE" == true ]] && [[ $EUID -ne 0 ]]; then
        error "System-wide install requires root. Use sudo or run without --system for user install."
    fi

    if [[ "$SYSTEM_MODE" == false ]] && [[ $EUID -eq 0 ]]; then
        warn "Running as root but installing in user mode."
        warn "This will install to /root - did you mean to use --system?"
        read -p "Continue? [y/N] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
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
            if [[ "$DISTRO_ID" == "centos" ]] || [[ "$DISTRO_ID" == "rhel" ]]; then
                sudo $PKG_INSTALL epel-release 2>/dev/null || true
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

# Install system dependencies (may need sudo)
install_dependencies() {
    step "Installing system dependencies..."

    if [[ "$PKG_MANAGER" == "nix" ]]; then
        if ! command -v python3 &>/dev/null; then
            warn "Python3 not found. On NixOS, add python3 to your configuration.nix"
            warn "Or run: nix-env -iA nixpkgs.python3"
        fi
        return
    fi

    # Determine if we need sudo for package installation
    local SUDO_CMD=""
    if [[ $EUID -ne 0 ]]; then
        SUDO_CMD="sudo"
    fi

    # Update package lists
    info "Updating package lists..."
    $SUDO_CMD $PKG_UPDATE 2>/dev/null || true

    # Install dependencies
    local packages="$PYTHON_PKG $PIP_PKG $DEPS"
    [[ -n "$VENV_PKG" ]] && packages="$packages $VENV_PKG"

    info "Installing: $packages"
    $SUDO_CMD $PKG_INSTALL $packages

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

# Create directories
create_directories() {
    step "Creating directories..."

    mkdir -p "$INSTALL_DIR"
    mkdir -p "$CONFIG_DIR"
    mkdir -p "$LOG_DIR"

    # For system mode, set ownership
    if [[ "$SYSTEM_MODE" == true ]]; then
        # Keep root ownership for system install
        true
    fi

    success "Directories created"
}

# Install backer-agent using pip
install_backer() {
    step "Installing backer-agent..."

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
    if [[ "$SYSTEM_MODE" == true ]]; then
        ln -sf "$INSTALL_DIR/venv/bin/backer" /usr/local/bin/backer
    else
        # User install - add to user's local bin
        mkdir -p "$USER_HOME/.local/bin"
        ln -sf "$INSTALL_DIR/venv/bin/backer" "$USER_HOME/.local/bin/backer"
    fi

    success "Backer installed to $INSTALL_DIR"
}

# Create systemd user service
create_user_service() {
    step "Creating systemd user service..."

    # Check if systemd user sessions are available
    if ! command -v systemctl &>/dev/null; then
        warn "Systemd not found - skipping service installation"
        return
    fi

    # Create user systemd directory
    local service_dir="$USER_HOME/.config/systemd/user"
    mkdir -p "$service_dir"

    cat > "$service_dir/backer-agent.service" << EOF
[Unit]
Description=Backer Backup Agent
Documentation=https://github.com/stocky789/backer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$INSTALL_DIR/venv/bin/backer agent start
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=backer-agent

# Environment
Environment="HOME=$USER_HOME"
Environment="XDG_CONFIG_HOME=$USER_HOME/.config"

[Install]
WantedBy=default.target
EOF

    # Enable lingering for user services (allows service to run without login)
    if command -v loginctl &>/dev/null; then
        if [[ $EUID -ne 0 ]]; then
            loginctl enable-linger "$CURRENT_USER" 2>/dev/null || \
            warn "Could not enable linger. Service will only run while logged in."
        else
            loginctl enable-linger "$CURRENT_USER" 2>/dev/null || true
        fi
    fi

    # Reload user daemon
    systemctl --user daemon-reload 2>/dev/null || true

    success "Systemd user service created"
}

# Create systemd system service (for --system mode)
create_system_service() {
    step "Creating systemd system service..."

    if ! command -v systemctl &>/dev/null; then
        warn "Systemd not found - skipping service installation"
        return
    fi

    cat > /etc/systemd/system/backer-agent.service << 'EOF'
[Unit]
Description=Backer Backup Agent
Documentation=https://github.com/stocky789/backer
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

# Run as root to access all files
User=root
Group=root

# Security (minimal restrictions since we need file access)
NoNewPrivileges=false
ProtectSystem=false
ProtectHome=false
PrivateTmp=false

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload

    success "Systemd system service created"
}

# Run setup wizard
run_wizard() {
    if [[ "$NO_WIZARD" == true ]]; then
        return
    fi

    echo
    step "Running setup wizard..."
    echo

    # Run the Python wizard
    "$INSTALL_DIR/venv/bin/backer" agent setup
}

# Uninstall function
uninstall() {
    set_paths

    info "Uninstalling Backer Agent..."

    # Stop and disable services
    if [[ "$SYSTEM_MODE" == true ]]; then
        if command -v systemctl &>/dev/null; then
            systemctl stop backer-agent 2>/dev/null || true
            systemctl disable backer-agent 2>/dev/null || true
            rm -f /etc/systemd/system/backer-agent.service
            systemctl daemon-reload
        fi
        rm -f /usr/local/bin/backer
    else
        if command -v systemctl &>/dev/null; then
            systemctl --user stop backer-agent 2>/dev/null || true
            systemctl --user disable backer-agent 2>/dev/null || true
            rm -f "$USER_HOME/.config/systemd/user/backer-agent.service"
            systemctl --user daemon-reload 2>/dev/null || true
        fi
        rm -f "$USER_HOME/.local/bin/backer"
    fi

    # Remove installation directory
    rm -rf "$INSTALL_DIR"
    rm -rf "$LOG_DIR"

    # Prompt for config removal
    if [[ -d "$CONFIG_DIR" ]]; then
        read -p "Remove configuration in $CONFIG_DIR? [y/N] " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            rm -rf "$CONFIG_DIR"
        fi
    fi

    success "Backer Agent uninstalled"
}

# Print post-install instructions
print_instructions() {
    echo
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}  Backer Agent Installed Successfully!${NC}"
    echo -e "${GREEN}========================================${NC}"
    echo
    echo "Installation: $INSTALL_DIR"
    echo "Configuration: $CONFIG_DIR"
    echo "Logs: $LOG_DIR"
    echo

    if [[ "$SYSTEM_MODE" == true ]]; then
        echo -e "${BLUE}Mode: System-wide (can backup all files)${NC}"
        echo
        echo -e "${BLUE}Start the agent:${NC}"
        echo "  sudo systemctl start backer-agent"
        echo
        echo -e "${BLUE}Enable on boot:${NC}"
        echo "  sudo systemctl enable backer-agent"
        echo
        echo -e "${BLUE}View logs:${NC}"
        echo "  journalctl -u backer-agent -f"
    else
        echo -e "${BLUE}Mode: User (can backup your personal files)${NC}"
        echo
        # Check if ~/.local/bin is in PATH
        if [[ ":$PATH:" != *":$USER_HOME/.local/bin:"* ]]; then
            echo -e "${YELLOW}Add to your PATH:${NC}"
            echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
            echo "  (Add this to ~/.bashrc or ~/.zshrc)"
            echo
        fi
        echo -e "${BLUE}Start the agent:${NC}"
        echo "  systemctl --user start backer-agent"
        echo
        echo -e "${BLUE}Enable on boot:${NC}"
        echo "  systemctl --user enable backer-agent"
        echo
        echo -e "${BLUE}View logs:${NC}"
        echo "  journalctl --user -u backer-agent -f"
    fi

    echo
    echo -e "${BLUE}Useful commands:${NC}"
    echo "  backer agent status    - Show agent status"
    echo "  backer agent setup     - Run setup wizard again"
    echo "  backer tools           - List installed backup tools"
    echo
}

# Main installation flow
main() {
    echo
    echo -e "${BLUE}╔═══════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║     Backer Agent Installer v1.1       ║${NC}"
    echo -e "${BLUE}╚═══════════════════════════════════════╝${NC}"
    echo

    set_paths

    if [[ "$UNINSTALL" == true ]]; then
        uninstall
        exit 0
    fi

    check_requirements

    if [[ "$SYSTEM_MODE" == true ]]; then
        info "Installing in SYSTEM mode (root access, can backup all files)"
    else
        info "Installing in USER mode (personal files only)"
        info "Use --system flag to install with full file system access"
    fi
    echo

    detect_distro
    setup_package_manager
    install_dependencies
    check_python
    create_directories
    install_backer

    if [[ "$SYSTEM_MODE" == true ]]; then
        create_system_service
    else
        create_user_service
    fi

    run_wizard
    print_instructions
}

# Run main
main "$@"
