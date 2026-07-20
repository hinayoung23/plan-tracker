#!/bin/bash
# Pre-release check — run before tagging any version.
# Fails with non-zero exit code if any test fails.
set -e
cd "$(dirname "$0")"

echo "=== Smoke tests ==="
python3 test_smoke.py

echo ""
echo "=== Integration tests ==="
python3 test_integration.py

echo ""
echo "=== JS syntax ==="
node -c src/index.js

echo ""
echo "=== Version sync ==="
PY_VER=$(python3 -c "from plan_tracker import __version__; print(__version__)")
JSON_VER=$(python3 -c "import json; print(json.load(open('package.json'))['version'])")
PLUGIN_VER=$(python3 -c "import json; print(json.load(open('openclaw.plugin.json'))['version'])")
TOML_VER=$(grep 'version =' pyproject.toml | head -1 | sed 's/.*"\(.*\)".*/\1/')
echo "  __init__.py:       $PY_VER"
echo "  package.json:      $JSON_VER"
echo "  openclaw.plugin:   $PLUGIN_VER"
echo "  pyproject.toml:    $TOML_VER"
[ "$PY_VER" = "$JSON_VER" ] && [ "$JSON_VER" = "$PLUGIN_VER" ] && [ "$PLUGIN_VER" = "$TOML_VER" ] || {
    echo "ERROR: version mismatch"
    exit 1
}

echo ""

# Verify data dir permissions
DATA_PERMS=$(stat -f '%Lp' "$(python3 -c "from plan_tracker.storage import DATA_DIR; print(DATA_DIR)" 2>/dev/null)" 2>/dev/null || echo "unknown")
echo "  data/ permissions: $DATA_PERMS"
[ "$DATA_PERMS" = "700" ] || {
    echo "ERROR: data directory permissions must be 700"
    exit 1
}

echo ""
echo "=== All checks passed ==="
