#!/usr/bin/env bash
#
# Backer Agent Uninstaller for Linux
# Auto-detects installation mode (user or system)
#
# Usage:
#   ./uninstall-agent.sh
#   or
#   curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/scripts/uninstall-agent.sh | bash
#
# Options:
#   --keep-registration   Keep agent registered on server (don't suggest removal)
#   --yes                 Skip confirmation prompts
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# Parse arguments
KEEP_REGISTRATION=false
YES=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --keep-registration)
            KEEP_REGISTRATION=true
            shift
            ;;
        --yes|-y)
            YES=true
            shift
            ;;
        *)
            shift
            ;;
    esac
done

echo ""
echo "========================================"
echo "       Backer Agent Uninstaller"
echo "========================================"
echo ""

# Detect installation mode
USER_INSTALL_DIR="$HOME/.local/share/backer"
SYSTEM_INSTALL_DIR="/opt/backer"

if [[ -d "$SYSTEM_INSTALL_DIR" ]] && [[ $EUID -eq 0 ]]; then
    MODE="system"
    INSTALL_DIR="$SYSTEM_INSTALL_DIR"
    CONFIG_DIR="/etc/backer"
    LOG_DIR="/var/log/backer"
    info "Detected: System-wide installation"
elif [[ -d "$USER_INSTALL_DIR" ]]; then
    MODE="user"
    INSTALL_DIR="$USER_INSTALL_DIR"
    CONFIG_DIR="$HOME/.config/backer"
    LOG_DIR="$HOME/.local/share/backer/logs"
    info "Detected: User installation"
elif [[ -d "$SYSTEM_INSTALL_DIR" ]] && [[ $EUID -ne 0 ]]; then
    error "System installation detected. Please run with sudo: sudo $0"
else
    warn "No Backer agent installation found."
    echo ""
    echo "Checked locations:"
    echo "  - User: $USER_INSTALL_DIR"
    echo "  - System: $SYSTEM_INSTALL_DIR"
    exit 0
fi

# Confirm uninstall
if [[ "$YES" != true ]]; then
    echo ""
    read -p "Uninstall Backer Agent from $INSTALL_DIR? [y/N] " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Cancelled."
        exit 0
    fi
fi

info "Stopping services..."

# Stop and disable services
if [[ "$MODE" == "system" ]]; then
    if command -v systemctl &>/dev/null; then
        systemctl stop backer-agent 2>/dev/null || true
        systemctl disable backer-agent 2>/dev/null || true
        rm -f /etc/systemd/system/backer-agent.service
        systemctl daemon-reload
        success "Systemd service removed"
    fi
    rm -f /usr/local/bin/backer
else
    if command -v systemctl &>/dev/null; then
        systemctl --user stop backer-agent 2>/dev/null || true
        systemctl --user disable backer-agent 2>/dev/null || true
        rm -f "$HOME/.config/systemd/user/backer-agent.service"
        systemctl --user daemon-reload 2>/dev/null || true
        success "Systemd user service removed"
    fi
    rm -f "$HOME/.local/bin/backer"
fi

# Remove installation directory
info "Removing installation files..."
rm -rf "$INSTALL_DIR"
rm -rf "$LOG_DIR"
success "Installation removed: $INSTALL_DIR"

# Always remove config to prevent stale credential issues
if [[ -d "$CONFIG_DIR" ]]; then
    info "Removing configuration..."
    rm -rf "$CONFIG_DIR"
    success "Configuration removed: $CONFIG_DIR"
fi

# Remove data directory (tools, cache, etc.) to prevent auth issues on reinstall
# This is critical - leftover data can cause 401 errors after reinstall
DATA_DIR="/root/.local/share/backer"
if [[ -d "$DATA_DIR" ]]; then
    info "Removing data directory..."
    rm -rf "$DATA_DIR"
    success "Data directory removed: $DATA_DIR"
fi

echo ""
success "Backer Agent has been uninstalled."
echo ""

# Show re-registration instructions
if [[ "$KEEP_REGISTRATION" != true ]]; then
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}  Re-installation Instructions${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "To reinstall and re-register this agent:"
    echo ""
    echo -e "  ${GREEN}1.${NC} Run the installer:"
    echo "     curl -fsSL https://raw.githubusercontent.com/stocky789/backer/main/scripts/install-agent.sh | sudo bash"
    echo ""
    echo -e "  ${GREEN}2.${NC} The agent will automatically re-register with the same hostname."
    echo "     Your backup jobs will continue to work."
    echo ""
    echo -e "${YELLOW}If you see authentication errors after reinstalling:${NC}"
    echo "  - Go to the Backer web UI → Agents"
    echo "  - Click 'Reset' on this agent to get new credentials"
    echo "  - Run the provided command on this machine"
    echo ""
fi
