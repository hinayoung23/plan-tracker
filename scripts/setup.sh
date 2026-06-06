#!/usr/bin/env bash
# One-command full installation for OpenClaw plugin
# Usage: bash setup.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$PLUGIN_DIR/.venv"

echo "=== plan-tracker plugin setup ==="
echo ""

# ---- resolve Python ----
PYTHON=""
if [ -f "$VENV_DIR/bin/python3" ]; then
    PYTHON="$VENV_DIR/bin/python3"
    echo "[0] Using existing virtual environment: $VENV_DIR"
else
    echo "[0] Creating virtual environment at $VENV_DIR..."
    if python3 -m venv "$VENV_DIR" 2>/dev/null; then
        PYTHON="$VENV_DIR/bin/python3"
        echo "     Created."
    else
        echo "     Failed. Falling back to system python3."
        PYTHON="python3"
    fi
fi

# ---- install Python package ----
echo "[1/3] Installing Python package..."
if [ "$PYTHON" != "python3" ]; then
    "$PYTHON" -m pip install --quiet -e "$PLUGIN_DIR"
else
    # Homebrew-managed system Python needs special handling
    python3 -m pip install --quiet --break-system-packages -e "$PLUGIN_DIR" 2>/dev/null || \
    python3 -m pip install --quiet --break-system-packages "$PLUGIN_DIR" 2>/dev/null || {
        echo "ERROR: pip install failed."
        echo "Try creating a venv manually: python3 -m venv $VENV_DIR"
        exit 1
    }
fi
echo "       Done."

# ---- run automated setup ----
echo "[2/3] Running setup (MCP registration + launchd + daemon)..."
"$PYTHON" -m plan_tracker.cli setup
echo ""

# ---- reminder about cron ----
echo "[3/3] Next step — install the QQ notification cron job (optional):"
echo "       $PYTHON -m plan_tracker.cli cron-setup --qq-id <your-qq-hex-id>"
echo ""
echo "=== Setup complete ==="
echo "Restart OpenClaw to apply:"
echo "  launchctl unload ~/Library/LaunchAgents/ai.openclaw.gateway.plist"
echo "  launchctl load ~/Library/LaunchAgents/ai.openclaw.gateway.plist"
