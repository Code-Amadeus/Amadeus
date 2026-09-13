#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT="$PROJECT_ROOT/Amadeus Wallpaper.app"
FORCE=0
EXPECTED_BUNDLE_NAME="Amadeus Wallpaper.app"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output) OUTPUT="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    *) echo "Usage: $0 [--output /path/to/Amadeus Wallpaper.app] [--force]" >&2; exit 2 ;;
  esac
done

if [[ "$(basename "$OUTPUT")" != "$EXPECTED_BUNDLE_NAME" ]]; then
  echo "Refusing output path that is not named '$EXPECTED_BUNDLE_NAME': $OUTPUT" >&2
  exit 1
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This app bundle can only be built on macOS." >&2
  exit 1
fi
for required in clang npm plutil codesign; do
  command -v "$required" >/dev/null || { echo "Missing required command: $required" >&2; exit 1; }
done
if [[ ! -x "$PROJECT_ROOT/.venv/bin/python3" ]]; then
  echo "Missing $PROJECT_ROOT/.venv; run uv sync first." >&2
  exit 1
fi
if [[ ! -d "$PROJECT_ROOT/electron/node_modules/electron" ]]; then
  echo "Missing Electron dependencies; run npm install in electron/." >&2
  exit 1
fi
is_generated_bundle() {
  local target="$1"
  local plist="$target/Contents/Info.plist"
  local executable="$target/Contents/MacOS/Amadeus Wallpaper"
  local project_root_file="$target/Contents/Resources/project-root"
  [[ -d "$target" && ! -L "$target" && -f "$plist" && -x "$executable" && -f "$project_root_file" ]] || return 1
  [[ "$(plutil -extract CFBundleIdentifier raw -o - "$plist" 2>/dev/null || true)" == "com.amadeus.wallpaper" ]] || return 1
  [[ "$(plutil -extract CFBundleExecutable raw -o - "$plist" 2>/dev/null || true)" == "Amadeus Wallpaper" ]]
}

validate_replacement() {
  [[ -e "$OUTPUT" || -L "$OUTPUT" ]] || return 0
  if [[ "$FORCE" != 1 ]]; then
    echo "$OUTPUT already exists; pass --force to replace this generated bundle." >&2
    exit 1
  fi
  if ! is_generated_bundle "$OUTPUT"; then
    echo "Refusing to replace an existing path that is not a generated Amadeus Wallpaper.app bundle: $OUTPUT" >&2
    exit 1
  fi
}

validate_replacement

echo "Building Electron production files..."
npm --prefix "$PROJECT_ROOT/electron" run build

STAGING_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/amadeus-wallpaper-app.XXXXXX")"
STAGING="$STAGING_ROOT/Amadeus Wallpaper.app"
trap 'rm -rf "$STAGING_ROOT"' EXIT
mkdir -p "$STAGING/Contents/MacOS" "$STAGING/Contents/Resources"

clang -std=c11 -Wall -Wextra -Werror "$SCRIPT_DIR/launcher.c" \
  -o "$STAGING/Contents/MacOS/Amadeus Wallpaper"
cp "$PROJECT_ROOT/assets/icons/app/app_icon.icns" "$STAGING/Contents/Resources/app_icon.icns"
printf '%s\n' "$PROJECT_ROOT" > "$STAGING/Contents/Resources/project-root"

cat > "$STAGING/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleDisplayName</key><string>Amadeus Wallpaper</string>
  <key>CFBundleExecutable</key><string>Amadeus Wallpaper</string>
  <key>CFBundleIconFile</key><string>app_icon.icns</string>
  <key>CFBundleIdentifier</key><string>com.amadeus.wallpaper</string>
  <key>CFBundleName</key><string>Amadeus Wallpaper</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

plutil -lint "$STAGING/Contents/Info.plist"
codesign --force --sign - "$STAGING"
mkdir -p "$(dirname "$OUTPUT")"
validate_replacement
if [[ -e "$OUTPUT" || -L "$OUTPUT" ]]; then rm -rf "$OUTPUT"; fi
mv "$STAGING" "$OUTPUT"
trap - EXIT
rm -rf "$STAGING_ROOT"
echo "Built $OUTPUT"
echo "Project root: $PROJECT_ROOT"
