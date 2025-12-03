#!/bin/bash
#
# Backer Server Uninstall Script
# Usage: sudo ./uninstall-server.sh [--keep-data]
#
# This script removes the Backer server from your system.
# Run with --keep-data to preserve backup configuration and history.
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m' # No Color

# Parse arguments
KEEP_DATA=false
SKIP_CONFIRM=false

for arg in "$@"; do
    case $arg in
        --keep-data)
            KEEP_DATA=true
            shift
            ;;
        -y|--yes)
            SKIP_CONFIRM=true
            shift
            ;;
        -h|--help)
            echo "Usage: sudo $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --keep-data    Preserve backup data in /var/lib/backer"
            echo "  -y, --yes      Skip confirmation prompt"
            echo "  -h, --help     Show this help message"
            exit 0
            ;;
    esac
done

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}Error:${NC} This script must be run as root"
    echo "Run with: sudo $0"
    exit 1
fi

echo -e "${BOLD}Backer Server Uninstall${NC}"
echo ""
echo "This will remove:"
echo "  • Systemd service (backer.service)"
echo "  • Installation directory (/opt/backer)"
echo "  • Binary symlink (/usr/local/bin/backer)"
if [[ "$KEEP_DATA" == "false" ]]; then
    echo -e "  • ${YELLOW}Backup data and config (/var/lib/backer)${NC}"
else
    echo -e "  • ${DIM}Backup data will be preserved${NC}"
fi
echo "  • System user (backer)"
echo ""

# Confirm
if [[ "$SKIP_CONFIRM" == "false" ]]; then
    read -p "Continue with uninstall? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
fi

echo ""
echo -e "${BOLD}1. Stopping service...${NC}"

# Stop service
if systemctl stop backer 2>/dev/null; then
    echo -e "   ${GREEN}✓${NC} Service stopped"
else
    echo -e "   ${DIM}Service not running or not found${NC}"
fi

# Disable service
if systemctl disable backer 2>/dev/null; then
    echo -e "   ${GREEN}✓${NC} Service disabled"
else
    echo -e "   ${DIM}Service not enabled or not found${NC}"
fi

echo ""
echo -e "${BOLD}2. Removing service file...${NC}"

# Remove service file
if [[ -f /etc/systemd/system/backer.service ]]; then
    rm -f /etc/systemd/system/backer.service
    echo -e "   ${GREEN}✓${NC} Removed /etc/systemd/system/backer.service"
    systemctl daemon-reload
    echo -e "   ${GREEN}✓${NC} Reloaded systemd"
else
    echo -e "   ${DIM}Service file not found${NC}"
fi

echo ""
echo -e "${BOLD}3. Removing installation files...${NC}"

# Remove /opt/backer
if [[ -d /opt/backer ]]; then
    rm -rf /opt/backer
    echo -e "   ${GREEN}✓${NC} Removed /opt/backer"
else
    echo -e "   ${DIM}/opt/backer not found${NC}"
fi

# Remove /var/lib/backer (unless --keep-data)
if [[ "$KEEP_DATA" == "false" ]]; then
    if [[ -d /var/lib/backer ]]; then
        rm -rf /var/lib/backer
        echo -e "   ${GREEN}✓${NC} Removed /var/lib/backer"
    else
        echo -e "   ${DIM}/var/lib/backer not found${NC}"
    fi
fi

# Remove symlink
if [[ -L /usr/local/bin/backer ]] || [[ -f /usr/local/bin/backer ]]; then
    rm -f /usr/local/bin/backer
    echo -e "   ${GREEN}✓${NC} Removed /usr/local/bin/backer"
else
    echo -e "   ${DIM}/usr/local/bin/backer not found${NC}"
fi

echo ""
echo -e "${BOLD}4. Removing system user...${NC}"

# Remove user
if userdel backer 2>/dev/null; then
    echo -e "   ${GREEN}✓${NC} Removed backer user"
else
    echo -e "   ${DIM}User not found or could not be removed${NC}"
fi

echo ""
echo -e "${BOLD}${GREEN}✓ Backer server uninstalled successfully${NC}"

if [[ "$KEEP_DATA" == "true" ]]; then
    echo ""
    echo -e "${YELLOW}Note:${NC} Backup data preserved in /var/lib/backer"
    echo "To remove it later: sudo rm -rf /var/lib/backer"
fi

echo ""
echo -e "${DIM}To clean up user files, run as your regular user:${NC}"
echo -e "${DIM}  rm -rf ~/backer ~/venv${NC}"
