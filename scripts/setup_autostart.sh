#!/bin/bash
# Manage macOS Boot Autostart for Amadeus Wallpaper Mode
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLIST_LABEL="com.amadeus.wallpaper"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
LOGS_DIR="$PROJECT_ROOT/logs"
if [ -d "/Applications/Amadeus Wallpaper.app" ]; then
  APP_PATH="/Applications/Amadeus Wallpaper.app"
else
  APP_PATH="$PROJECT_ROOT/Amadeus Wallpaper.app"
fi

ACTION="${1:-status}"

mkdir -p "$LOGS_DIR"

enable_launchagent() {
  echo "==> Configuring macOS LaunchAgent for Amadeus Wallpaper..."
  mkdir -p "$HOME/Library/LaunchAgents"

  # If already loaded, unload first
  if launchctl list | grep -q "$PLIST_LABEL"; then
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
  fi

  cat << PLIST > "$PLIST_PATH"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${APP_PATH}/Contents/MacOS/Amadeus Wallpaper</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>WorkingDirectory</key>
    <string>${PROJECT_ROOT}</string>
    <key>StandardOutPath</key>
    <string>${LOGS_DIR}/wallpaper_autostart.log</string>
    <key>StandardErrorPath</key>
    <string>${LOGS_DIR}/wallpaper_autostart_err.log</string>
</dict>
</plist>
PLIST

  launchctl load "$PLIST_PATH"
  echo "✅ LaunchAgent enabled successfully!"
  echo "   Plist: $PLIST_PATH"
  echo "   Logs:  $LOGS_DIR/wallpaper_autostart.log"
}

disable_launchagent() {
  echo "==> Disabling macOS LaunchAgent..."
  if [ -f "$PLIST_PATH" ]; then
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    echo "✅ LaunchAgent removed."
  else
    echo "LaunchAgent plist not found; nothing to remove."
  fi
}

enable_login_item() {
  echo "==> Adding Amadeus Wallpaper.app to macOS Login Items..."
  if [ ! -d "$APP_PATH" ]; then
    echo "Error: $APP_PATH not found. Please build it first." >&2
    exit 1
  fi
  osascript -e "tell application \"System Events\" to delete (every login item whose name is \"Amadeus Wallpaper\")" 2>/dev/null || true
  osascript -e "tell application \"System Events\" to make login item at end with properties {path:\"${APP_PATH}\", hidden:true, name:\"Amadeus Wallpaper\"}"
  echo "✅ Amadeus Wallpaper.app added to macOS Login Items!"
  echo "   Check: System Settings -> General -> Login Items"
}

disable_login_item() {
  echo "==> Removing Amadeus Wallpaper from macOS Login Items..."
  osascript -e "tell application \"System Events\" to delete (every login item whose name is \"Amadeus Wallpaper\")" 2>/dev/null || true
  echo "✅ Removed from Login Items."
}

show_status() {
  echo "=== Amadeus Wallpaper Autostart Status ==="
  echo "1. LaunchAgent status:"
  if [ -f "$PLIST_PATH" ]; then
    echo "   Plist exists: $PLIST_PATH"
    if launchctl list | grep -q "$PLIST_LABEL"; then
      echo "   Status: LOADED / ACTIVE"
    else
      echo "   Status: Installed but not loaded"
    fi
  else
    echo "   Status: NOT INSTALLED"
  fi

  echo "2. macOS Login Items status:"
  ITEMS=$(osascript -e 'tell application "System Events" to get name of every login item' 2>/dev/null || echo "")
  if echo "$ITEMS" | grep -q "Amadeus Wallpaper"; then
    echo "   Status: FOUND in Login Items ('Amadeus Wallpaper')"
  else
    echo "   Status: NOT in Login Items"
  fi
}

case "$ACTION" in
  enable|install)
    enable_launchagent
    ;;
  disable|uninstall)
    disable_launchagent
    ;;
  enable-app|login-item)
    enable_login_item
    ;;
  disable-app)
    disable_login_item
    ;;
  status)
    show_status
    ;;
  *)
    echo "Usage: $0 {enable|disable|enable-app|disable-app|status}"
    echo "  enable      : Install background LaunchAgent (recommended for headless wallpaper startup)"
    echo "  disable     : Remove background LaunchAgent"
    echo "  enable-app  : Add Amadeus Wallpaper.app to macOS System Settings Login Items"
    echo "  disable-app : Remove Amadeus Wallpaper.app from Login Items"
    echo "  status      : Show current autostart status"
    exit 1
    ;;
esac
