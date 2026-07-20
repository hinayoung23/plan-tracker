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
PACKAGE_VERSION="$(sed -n 's/^[[:space:]]*version = "\([^"]*\)"/\1/p' "$PLUGIN_DIR/pyproject.toml" | head -n 1)"
WHEEL_PATH="$PLUGIN_DIR/dist/plan_tracker-$PACKAGE_VERSION-py3-none-any.whl"

if [ "$PYTHON" != "python3" ]; then
    "$PYTHON" -c 'import mcp' 2>/dev/null || \
        "$PYTHON" -m pip install --quiet 'mcp>=1.0.0'
    if [ -f "$WHEEL_PATH" ]; then
        "$PYTHON" -m pip install --quiet --force-reinstall --no-deps "$WHEEL_PATH"
    else
        "$PYTHON" -m pip install --quiet --force-reinstall --no-deps "$PLUGIN_DIR" || \
        "$PYTHON" -m pip install --quiet --force-reinstall --no-deps --no-build-isolation "$PLUGIN_DIR"
    fi
else
    # Homebrew-managed system Python needs special handling
    python3 -c 'import mcp' 2>/dev/null || \
        python3 -m pip install --quiet --break-system-packages 'mcp>=1.0.0'
    INSTALL_TARGET="$PLUGIN_DIR"
    if [ -f "$WHEEL_PATH" ]; then
        INSTALL_TARGET="$WHEEL_PATH"
    fi
    python3 -m pip install --quiet --break-system-packages --force-reinstall --no-deps "$INSTALL_TARGET" 2>/dev/null || \
    python3 -m pip install --quiet --break-system-packages --force-reinstall --no-deps --no-build-isolation "$PLUGIN_DIR" 2>/dev/null || {
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
echo "[3/3] Next step — install the notification cron job (optional):"
echo "       $PYTHON -m plan_tracker.cli cron-setup"
echo "       If auto-detection is unavailable, pass --delivery-config <0600-json-path>."
echo ""
echo "=== Setup complete ==="
echo "Restart OpenClaw to apply:"
echo "  openclaw gateway restart"
