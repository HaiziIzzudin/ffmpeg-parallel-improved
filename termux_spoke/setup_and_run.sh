#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
#  FFmpeg Distributed Spoke — Termux Setup & Auto-Launcher
# ============================================================
#
# Run this script once to set up the Termux environment and
# launch the spoke. It is idempotent — running it again will
# update dependencies and re-launch.
#
# Usage:
#   bash setup_and_run.sh
#   bash setup_and_run.sh --manual 192.168.1.100
#   bash setup_and_run.sh --port 8000
#
# Dependencies installed automatically:
#   - python, ffmpeg, termux-api (via pkg)
#   - rich, websockets, httpx, zeroconf (via pip in venv)
# ============================================================

set -euo pipefail

# ── Paths ──────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$HOME/ffmpeg-spoke"
VENV_DIR="$WORK_DIR/venv"
CONFIG_FILE="$WORK_DIR/config.env"
LOG_DIR="$WORK_DIR/logs"

# ── Colors (for the setup prompts only) ─────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}  FFmpeg Distributed Spoke — Termux Setup${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# ── Parse CLI args ─────────────────────────────────────────
MANUAL_IP=""
HUB_PORT=8000

while [[ $# -gt 0 ]]; do
    case "$1" in
        --manual) MANUAL_IP="$2"; shift 2 ;;
        --port)   HUB_PORT="$2"; shift 2 ;;
        *)        echo -e "${RED}Unknown option: $1${NC}"; exit 1 ;;
    esac
done

# ── Step 1: Create work directory ──────────────────────────
echo -e "${YELLOW}[1/5]${NC} Setting up work directory..."
mkdir -p "$WORK_DIR" "$LOG_DIR"

# ── Step 2: Install system packages ────────────────────────
echo -e "${YELLOW}[2/5]${NC} Installing system packages..."
pkg update -y
pkg install -y python ffmpeg termux-api 2>&1 | tail -3

# Verify ffmpeg is available
if ! command -v ffmpeg &>/dev/null; then
    echo -e "${RED}ERROR: ffmpeg not found after installation. Try: pkg install ffmpeg${NC}"
    exit 1
fi
echo -e "  ${GREEN}✓${NC} ffmpeg $(ffmpeg -version 2>&1 | head -1 | grep -oP 'version \K[0-9.]+'||true)"

# ── Step 3: Copy spoke source files ────────────────────────
echo -e "${YELLOW}[3/5]${NC} Copying spoke source files into package directory..."
PKG_DIR="$WORK_DIR/termux_spoke"
mkdir -p "$PKG_DIR"
if [[ ! -f "$PKG_DIR/__main__.py" ]]; then
    cp "$SCRIPT_DIR"/*.py "$PKG_DIR/"
    cp "$SCRIPT_DIR"/requirements.txt "$WORK_DIR/"
    echo -e "  ${GREEN}✓${NC} Source files copied to $PKG_DIR"
else
    # Update only changed files (compare timestamps)
    for f in "$SCRIPT_DIR"/*.py; do
        dest="$PKG_DIR/$(basename "$f")"
        if [[ "$f" -nt "$dest" ]]; then
            cp "$f" "$dest"
        fi
    done
    # Update requirements.txt separately (kept at WORK_DIR level)
    if [[ "$SCRIPT_DIR/requirements.txt" -nt "$WORK_DIR/requirements.txt" ]]; then
        cp "$SCRIPT_DIR"/requirements.txt "$WORK_DIR/"
    fi
    echo -e "  ${GREEN}✓${NC} Source files up to date"
fi

# ── Step 4: Create virtualenv and install deps ─────────────
echo -e "${YELLOW}[4/5]${NC} Setting up Python virtual environment..."

if [[ ! -d "$VENV_DIR" ]]; then
    python -m venv "$VENV_DIR"
    echo -e "  ${GREEN}✓${NC} Virtual environment created"
fi

# Activate and install
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

pip install --quiet --upgrade pip 2>&1 | tail -1
pip install --quiet -r "$WORK_DIR/requirements.txt" 2>&1 | tail -1
echo -e "  ${GREEN}✓${NC} Python dependencies installed"

# ── Step 5: Configuration ──────────────────────────────────
echo -e "${YELLOW}[5/5]${NC} Configuration..."

# CLI --manual takes precedence over config file
if [[ -n "$MANUAL_IP" ]]; then
    echo -e "  Using CLI-provided IP: ${CYAN}$MANUAL_IP${NC}"
    cat > "$CONFIG_FILE" <<EOF
MANUAL_IP=$MANUAL_IP
HUB_PORT=$HUB_PORT
EOF
elif [[ -f "$CONFIG_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$CONFIG_FILE"
    echo -e "  ${GREEN}✓${NC} Loaded saved config (${CONFIG_FILE})"
else
    echo ""
    echo -e "  ${CYAN}No saved configuration found.${NC}"
    echo -e "  ${CYAN}Enter PC Hub IP address${NC} (leave blank for auto-discovery):"
    echo -n "  → "
    read -r MANUAL_IP
    echo ""
    cat > "$CONFIG_FILE" <<EOF
MANUAL_IP=$MANUAL_IP
HUB_PORT=$HUB_PORT
EOF
    echo -e "  ${GREEN}✓${NC} Config saved to ${CONFIG_FILE}"
fi

# ── Acquire Termux wake lock ───────────────────────────────
echo -e ""
echo -e "${BOLD}Acquiring wake lock...${NC}"
termux-wake-lock 2>/dev/null || echo -e "  ${YELLOW}⚠ termux-wake-lock unavailable (install termux-api)${NC}"

# ── Launch! ────────────────────────────────────────────────
echo -e ""
echo -e "${GREEN}━━━━━ Launching Spoke ─━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e ""

cd "$WORK_DIR"
ARGS=()
[[ -n "$MANUAL_IP" ]] && ARGS+=(--manual "$MANUAL_IP")
ARGS+=(--port "$HUB_PORT")

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m termux_spoke "${ARGS[@]}"

# ── Cleanup on exit ────────────────────────────────────────
RET=$?
termux-wake-unlock 2>/dev/null || true
echo -e ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}  Spoke shut down (exit code: $RET)${NC}"
echo -e "${CYAN}  Logs: $LOG_DIR/spoke.log${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
exit $RET