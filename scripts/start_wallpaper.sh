#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${AMADEUS_PROJECT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python3"
ELECTRON_BIN="$PROJECT_ROOT/electron/node_modules/.bin/electron"
LOG_DIR="$HOME/Library/Logs/Amadeus"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Amadeus Wallpaper.app is supported only on macOS." >&2
  exit 1
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python environment not found at $PYTHON_BIN; run uv sync first." >&2
  exit 1
fi
if [[ ! -x "$ELECTRON_BIN" ]]; then
  echo "Electron is not installed at $ELECTRON_BIN; run npm install in electron/." >&2
  exit 1
fi
if [[ ! -f "$PROJECT_ROOT/electron/dist/main/index.js" ]]; then
  echo "Electron production build is missing; run scripts/build_macos_wallpaper_app.sh." >&2
  exit 1
fi

mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/wallpaper.log" 2>&1
echo
echo "[$(date -Iseconds)] starting Amadeus Wallpaper from $PROJECT_ROOT"

export AMADEUS_PROJECT_ROOT="$PROJECT_ROOT"
export AMADEUS_PYTHON="$PYTHON_BIN"
export NODE_ENV=production
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

# Electron owns its backend process. Unknown listeners are rejected by the host
# and are never killed by this launcher.
exec "$ELECTRON_BIN" "$PROJECT_ROOT/electron" --wallpaper "$@"
