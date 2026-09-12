#!/bin/bash
# Start Amadeus in background wallpaper mode on macOS
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

export NODE_ENV=production
export AMADEUS_PYTHON="$PROJECT_ROOT/.venv/bin/python3"
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

mkdir -p "$PROJECT_ROOT/logs"

# Clean up orphaned backend on port 17777 only if no active Electron instance is running
if ! pgrep -f "Electron.*amadeus" >/dev/null 2>&1 && ! pgrep -f "Electron.*electron" >/dev/null 2>&1; then
  STALE_PORT_PID=$(lsof -nP -ti :17777 2>/dev/null || true)
  if [ -n "$STALE_PORT_PID" ]; then
    echo "[start_wallpaper] Terminating orphaned backend on port 17777 (PID: $STALE_PORT_PID)..."
    kill $STALE_PORT_PID 2>/dev/null || true
    sleep 1
  fi
fi

ELECTRON_BIN="$PROJECT_ROOT/electron/node_modules/.bin/electron"
if [ ! -x "$ELECTRON_BIN" ]; then
  echo "Error: Electron binary not found at $ELECTRON_BIN" >&2
  exit 1
fi

exec "$ELECTRON_BIN" "$PROJECT_ROOT/electron" --wallpaper "$@"
