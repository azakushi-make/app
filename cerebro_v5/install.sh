#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════
#  CEREBRO QUANT v5 — Automatic Installer
#  Supports: Termux (Android) and standard Linux/macOS
# ══════════════════════════════════════════════════════════
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║      CEREBRO QUANT v5  —  INSTALLER             ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ── Detect environment ────────────────────────────────────
if [ -n "$TERMUX_VERSION" ]; then
    echo "[INFO] Detected: Termux (Android)"
    echo "[INFO] Updating Termux packages..."
    pkg update -y 2>/dev/null || true
    pkg install python3 python-pip -y 2>/dev/null || true
    PYTHON_CMD="python3"
elif command -v python3 &>/dev/null; then
    echo "[INFO] Detected: Linux / macOS"
    PYTHON_CMD="python3"
else
    echo "[ERROR] python3 not found. Please install Python 3.9+."
    exit 1
fi

echo "[INFO] Python: $($PYTHON_CMD --version)"

# ── Create virtual environment ────────────────────────────
echo ""
echo "[STEP 1/4] Creating virtual environment in env/ ..."
$PYTHON_CMD -m venv env

# Activate
if [ -f "env/bin/activate" ]; then
    # shellcheck disable=SC1091
    source env/bin/activate
elif [ -f "env/Scripts/activate" ]; then
    # Windows Git Bash
    # shellcheck disable=SC1091
    source env/Scripts/activate
fi

echo "[OK] Virtual environment created and activated"

# ── Install dependencies ──────────────────────────────────
echo ""
echo "[STEP 2/4] Installing dependencies from requirements.txt ..."
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
echo "[OK] Dependencies installed"

# ── Create directories ────────────────────────────────────
echo ""
echo "[STEP 3/4] Creating required directories ..."
mkdir -p brain logs
echo "[OK] Created: brain/  logs/"

# ── Copy config template if needed ───────────────────────
echo ""
echo "[STEP 4/4] Checking configuration ..."
if [ ! -f "config_alpaca.json" ]; then
    if [ -f "config_template.json" ]; then
        cp config_template.json config_alpaca.json
        echo "[OK] Copied config_template.json → config_alpaca.json"
        echo ""
        echo "  ┌──────────────────────────────────────────────────────┐"
        echo "  │  ACTION REQUIRED: Fill in your credentials           │"
        echo "  │                                                      │"
        echo "  │  Edit config_alpaca.json and replace all             │"
        echo "  │  YOUR_*_HERE placeholders with real values.          │"
        echo "  │                                                      │"
        echo "  │  nano config_alpaca.json                             │"
        echo "  └──────────────────────────────────────────────────────┘"
    else
        echo "[WARN] config_template.json not found. Create config_alpaca.json manually."
    fi
else
    echo "[OK] config_alpaca.json already exists"
fi

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   Installation complete.                         ║"
echo "║                                                  ║"
echo "║   Edit config_alpaca.json then run:              ║"
echo "║     source env/bin/activate                      ║"
echo "║     python main.py                               ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
