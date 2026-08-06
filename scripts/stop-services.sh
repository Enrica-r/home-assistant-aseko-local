#!/bin/bash
# Stop Home Assistant for Aseko Local development

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}Stopping Home Assistant...${NC}"
echo ""

# Stop Home Assistant
if [ -f /tmp/ha_server.pid ]; then
    PID=$(cat /tmp/ha_server.pid)
    if kill -0 "$PID" 2>/dev/null; then
        echo -e "${YELLOW}Stopping Home Assistant (PID: $PID)...${NC}"
        kill "$PID" 2>/dev/null || true
        sleep 2
        if kill -0 "$PID" 2>/dev/null; then
            echo -e "${YELLOW}Force killing Home Assistant...${NC}"
            kill -9 "$PID" 2>/dev/null || true
        fi
        rm -f /tmp/ha_server.pid
        echo -e "${GREEN}✓ Home Assistant stopped${NC}"
    else
        echo -e "${YELLOW}Home Assistant was not running${NC}"
        rm -f /tmp/ha_server.pid
    fi
else
    echo -e "${YELLOW}PID file not found${NC}"
fi

echo ""

# Clean up any remaining process on port 8123
echo -e "${YELLOW}Cleaning up any remaining processes...${NC}"
if command -v lsof &> /dev/null; then
    if lsof -Pi :8123 -sTCP:LISTEN -t >/dev/null 2>&1; then
        PID=$(lsof -Pi :8123 -sTCP:LISTEN -t)
        echo -e "${YELLOW}Found process on port 8123: $PID${NC}"
        kill "$PID" 2>/dev/null || true
        sleep 1
        kill -9 "$PID" 2>/dev/null || true
        echo -e "${GREEN}✓ Killed process on port 8123${NC}"
    fi
else
    echo -e "${YELLOW}lsof not available - cannot check port 8123${NC}"
fi

echo ""
echo -e "${GREEN}Home Assistant stopped${NC}"
echo -e "${BLUE}========================================${NC}"
