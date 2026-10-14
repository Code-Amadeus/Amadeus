#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_DIR="$PROJECT_ROOT/build/bin"
OUTPUT="$BUILD_DIR/mac_screen_ocr"
SOURCE="$SCRIPT_DIR/mac_screen_ocr.swift"
DIALOGUE_SOURCE="$SCRIPT_DIR/mac_ocr_dialogue.swift"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "mac_screen_ocr is supported only on macOS." >&2
  exit 1
fi

mkdir -p "$BUILD_DIR"

# Only recompile if source is newer than binary
if [[ ! -x "$OUTPUT" || "$SOURCE" -nt "$OUTPUT" || "$DIALOGUE_SOURCE" -nt "$OUTPUT" || "${BASH_SOURCE[0]}" -nt "$OUTPUT" ]]; then
  TEMP_OUTPUT="$(mktemp "$BUILD_DIR/.mac_screen_ocr.XXXXXX")"
  trap 'rm -f "$TEMP_OUTPUT"' EXIT
  swiftc -O "$SOURCE" "$DIALOGUE_SOURCE" -o "$TEMP_OUTPUT" -framework ScreenCaptureKit -framework Vision -framework AppKit -target "$(uname -m)-apple-macos14.0"
  mv -f "$TEMP_OUTPUT" "$OUTPUT"
  echo "mac_screen_ocr compiled successfully to $OUTPUT"
fi
