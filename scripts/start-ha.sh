#!/bin/bash
# Start Home Assistant for Aseko Local development

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
HA_CONFIG_DIR="$PROJECT_ROOT/config"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Aseko Local - Home Assistant Dev${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Create Home Assistant config directory if it doesn't exist
if [ ! -d "$HA_CONFIG_DIR" ]; then
    echo -e "${GREEN}Creating Home Assistant config directory...${NC}"
    mkdir -p "$HA_CONFIG_DIR"
fi

# Create configuration.yaml if it doesn't exist
if [ ! -f "$HA_CONFIG_DIR/configuration.yaml" ]; then
    echo -e "${GREEN}Creating default configuration.yaml...${NC}"
    cat > "$HA_CONFIG_DIR/configuration.yaml" <<'EOF'
# https://www.home-assistant.io/integrations/default_config/
default_config:

# https://www.home-assistant.io/integrations/homeassistant/
homeassistant:
  debug: true

# https://www.home-assistant.io/integrations/logger/
logger:
  default: info
  logs:
    custom_components.aseko_local: debug
EOF
    echo -e "${GREEN}✓ Default configuration.yaml created${NC}"
    echo ""
fi

# Create symlink to custom_components if it doesn't exist
if [ ! -e "$HA_CONFIG_DIR/custom_components" ]; then
    echo -e "${GREEN}Creating symlink to custom_components...${NC}"
    ln -s "$PROJECT_ROOT/custom_components" "$HA_CONFIG_DIR/custom_components"
    echo -e "${GREEN}✓ Symlink created${NC}"
    echo ""
fi

# Check if Home Assistant is already running
if curl -s "http://localhost:8123" > /dev/null 2>&1; then
    echo -e "${YELLOW}⚠ Home Assistant is already running on port 8123${NC}"
    echo -e "${YELLOW}  → http://localhost:8123${NC}"
    echo ""
else
    echo -e "${GREEN}Starting Home Assistant...${NC}"
    echo "Config directory: $HA_CONFIG_DIR"
    echo ""

    cd "$HA_CONFIG_DIR"
    python -m homeassistant -c "$HA_CONFIG_DIR" &
    HA_PID=$!
    echo $HA_PID > /tmp/ha_server.pid

    # Wait for Home Assistant to start
    echo "Waiting for Home Assistant to start (this may take 30-60 seconds)..."
    WAIT_COUNT=0
    while ! curl -s "http://localhost:8123" > /dev/null 2>&1; do
        sleep 1
        WAIT_COUNT=$((WAIT_COUNT + 1))
        if [ $WAIT_COUNT -gt 120 ]; then
            echo -e "${RED}✗ Home Assistant failed to start${NC}"
            echo "  Check the log: tail -f $HA_CONFIG_DIR/home-assistant.log"
            exit 1
        fi
        printf "\rWaiting... %ss" "$WAIT_COUNT"
    done
    echo ""
    echo -e "${GREEN}✓ Home Assistant started successfully${NC}"
    echo ""
fi

echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}Home Assistant is ready!${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "${YELLOW}Next steps:${NC}"
echo "1. Home Assistant UI: http://localhost:8123"
echo "2. Settings → Devices & Services → Add Integration"
echo "3. Search for 'Aseko Local' and select it"
echo "4. Enter the IP and port of your Aseko unit (default 47524)"
echo ""
echo -e "${YELLOW}To stop Home Assistant:${NC}"
echo "  bash scripts/stop-services.sh"
echo ""
echo -e "${YELLOW}To reset Home Assistant:${NC}"
echo "  bash scripts/reset-ha.sh"
echo ""
