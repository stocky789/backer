#!/bin/bash
# Quick test script for backer

set -e

echo "=== Backer Test Script ==="
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test directory
TEST_DIR="/tmp/backer-test-$$"
mkdir -p "$TEST_DIR"

cleanup() {
    echo ""
    echo "Cleaning up $TEST_DIR..."
    rm -rf "$TEST_DIR"
}
trap cleanup EXIT

echo "Test directory: $TEST_DIR"
echo ""

# Create test data
echo -e "${YELLOW}1. Creating test data...${NC}"
mkdir -p "$TEST_DIR/source/subdir"
echo "File 1 content" > "$TEST_DIR/source/file1.txt"
echo "File 2 content" > "$TEST_DIR/source/file2.txt"
echo "Nested content" > "$TEST_DIR/source/subdir/nested.txt"
dd if=/dev/urandom of="$TEST_DIR/source/random.bin" bs=1024 count=100 2>/dev/null
echo -e "${GREEN}✓ Created test files${NC}"
ls -la "$TEST_DIR/source/"
echo ""

# Check tools
echo -e "${YELLOW}2. Checking available tools...${NC}"
backer tools
echo ""

# Test rclone backup (local to local)
echo -e "${YELLOW}3. Testing rclone backend (local sync)...${NC}"
mkdir -p "$TEST_DIR/dest-rclone"
if backer backup "$TEST_DIR/source" "$TEST_DIR/dest-rclone" -b rclone; then
    echo -e "${GREEN}✓ rclone backup succeeded${NC}"
    ls -la "$TEST_DIR/dest-rclone/"
else
    echo -e "${RED}✗ rclone backup failed${NC}"
fi
echo ""

# Test dry run
echo -e "${YELLOW}4. Testing dry run...${NC}"
backer backup "$TEST_DIR/source" "$TEST_DIR/dest-dryrun" -b rclone --dry-run
echo -e "${GREEN}✓ Dry run completed${NC}"
echo ""

# Test with excludes
echo -e "${YELLOW}5. Testing backup with excludes...${NC}"
mkdir -p "$TEST_DIR/dest-exclude"
backer backup "$TEST_DIR/source" "$TEST_DIR/dest-exclude" -b rclone -e "*.bin"
echo "Files in destination (*.bin should be excluded):"
ls -la "$TEST_DIR/dest-exclude/"
echo ""

# Test restore
echo -e "${YELLOW}6. Testing restore...${NC}"
mkdir -p "$TEST_DIR/restored"
backer restore "$TEST_DIR/dest-rclone" "$TEST_DIR/restored" -b rclone
echo -e "${GREEN}✓ Restore completed${NC}"
echo "Restored files:"
ls -la "$TEST_DIR/restored/"
echo ""

# Verify restore
echo -e "${YELLOW}7. Verifying restored files...${NC}"
if diff "$TEST_DIR/source/file1.txt" "$TEST_DIR/restored/file1.txt" > /dev/null; then
    echo -e "${GREEN}✓ File content matches${NC}"
else
    echo -e "${RED}✗ File content mismatch${NC}"
fi
echo ""

echo -e "${GREEN}=== All tests completed ===${NC}"
