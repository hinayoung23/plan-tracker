#!/usr/bin/env bash
# One-command full installation for OpenClaw plugin
# Usage: bash setup.sh
set -e

echo "=== plan-tracker plugin setup ==="
echo ""

# 1. Install Python package from current directory
echo "[1/3] Installing Python package..."
python3 -m pip install --quiet -e . 2>/dev/null || python3 -m pip install --quiet .
echo "       Done."

# 2. Run the automated setup (MCP config + launchd + daemon)
echo "[2/3] Running setup (MCP registration + launchd + daemon)..."
python3 -m plan_tracker.cli setup
echo ""

# 3. Remind about cron job
echo "[3/3] Next step — install the QQ notification cron job (optional):"
echo "       python3 -m plan_tracker.cli cron-setup --qq-id <your-qq-hex-id>"
echo ""
echo "=== Setup complete ==="
echo "Restart OpenClaw to apply:"
echo "  launchctl unload ~/Library/LaunchAgents/ai.openclaw.gateway.plist"
echo "  launchctl load ~/Library/LaunchAgents/ai.openclaw.gateway.plist"
