#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LABEL="com.amadeus.wallpaper"
DOMAIN="gui/$(id -u)"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/Amadeus"
ACTION="${1:-status}"
APP_PATH="${AMADEUS_WALLPAPER_APP:-$PROJECT_ROOT/Amadeus Wallpaper.app}"

if [[ "${2:-}" == "--app" && -n "${3:-}" ]]; then APP_PATH="$3"; fi
APP_PARENT="$(cd "$(dirname "$APP_PATH")" 2>/dev/null && pwd)"
APP_PATH="$APP_PARENT/$(basename "$APP_PATH")"
EXECUTABLE="$APP_PATH/Contents/MacOS/Amadeus Wallpaper"

enable_agent() {
  if [[ ! -x "$EXECUTABLE" ]]; then
    echo "App launcher not found at $EXECUTABLE" >&2
    echo "Build it first with scripts/build_macos_wallpaper_app.sh." >&2
    exit 1
  fi
  mkdir -p "$(dirname "$PLIST_PATH")" "$LOG_DIR"
  "$SCRIPT_DIR/write_macos_wallpaper_plist.py" \
    --label "$LABEL" --program "$EXECUTABLE" \
    --stdout "$LOG_DIR/autostart.log" --stderr "$LOG_DIR/autostart-error.log" \
    --output "$PLIST_PATH"
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$PLIST_PATH"
  launchctl enable "$DOMAIN/$LABEL"
  echo "Enabled $LABEL"
  echo "App: $APP_PATH"
  echo "Plist: $PLIST_PATH"
}

disable_agent() {
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST_PATH"
  echo "Disabled $LABEL"
}

show_status() {
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "loaded: $LABEL"
  elif [[ -f "$PLIST_PATH" ]]; then
    echo "installed but not loaded: $PLIST_PATH"
  else
    echo "not installed: $LABEL"
  fi
  [[ -f "$PLIST_PATH" ]] && plutil -p "$PLIST_PATH"
}

case "$ACTION" in
  enable|install) enable_agent ;;
  disable|uninstall) disable_agent ;;
  status) show_status ;;
  *) echo "Usage: $0 {enable|disable|status} [--app /path/to/Amadeus Wallpaper.app]" >&2; exit 2 ;;
esac
