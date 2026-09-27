#!/usr/bin/env bash
# EverEvolving Trading Agent -- installer for macOS and Linux.
#
#   bash install.sh            (or double-click Install.command on a Mac)
#
# What it does, in order, and nothing else:
#   1. finds Python 3.11 or newer (and says exactly how to get it if there is none)
#   2. makes a private Python environment in your user data folder
#   3. installs the agent into it from this folder
#   4. adds "EverEvolving Trading Agent" to Applications (macOS) or the app menu
#      (Linux), and puts it on the Desktop
#   5. opens the app
#
# It never asks for a password or an API key. The app asks for your own keys
# later, on its Keys page, and keeps them in your system keychain.
set -euo pipefail

APP_NAME="EverEvolving Trading Agent"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "$(uname -s)" in
  Darwin) SYSTEM=mac; DATA="$HOME/Library/Application Support/EverEvolving" ;;
  *)      SYSTEM=linux; DATA="${XDG_DATA_HOME:-$HOME/.local/share}/everevolving" ;;
esac
VENV="$DATA/venv"
NO_LAUNCH=0
[ "${1:-}" = "--no-launch" ] && NO_LAUNCH=1

say()  { printf '  %s\n' "$*"; }
step() { printf '\n  > %s\n' "$*"; }

printf '\n  %s -- installing\n  from   %s\n  into   %s\n' "$APP_NAME" "$REPO" "$DATA"

step "1 of 5  Python"
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PYTHON="$(command -v "$candidate")"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  say "Python 3.11 or newer was not found."
  if [ "$SYSTEM" = mac ]; then
    say "Install it from https://www.python.org/downloads/macos/ (or: brew install python@3.12),"
  else
    say "Install it with your package manager (for example: sudo apt install python3 python3-venv),"
  fi
  say "then run this installer again."
  exit 1
fi
say "using $PYTHON"

step "2 of 5  A private environment for the agent"
mkdir -p "$DATA"
if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON" -m venv "$VENV" || {
    say "Could not create the environment. On Debian/Ubuntu: sudo apt install python3-venv"
    exit 1
  }
fi
say "$VENV"

step "3 of 5  Installing the agent (a minute or two the first time)"
"$VENV/bin/python" -m pip install --upgrade pip --quiet --disable-pip-version-check
"$VENV/bin/python" -m pip install -e "$REPO[all]" --quiet --disable-pip-version-check
"$VENV/bin/python" -c "import ee_agent.desktop"
say "installed"

step "4 of 5  The app icon"
ICON_PNG="$("$VENV/bin/python" -m ee_agent.ui.icon "$DATA" >/dev/null; echo "$DATA/everevolving.png")"
if [ "$SYSTEM" = mac ]; then
  APP="$HOME/Applications/$APP_NAME.app"
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
  cat > "$APP/Contents/MacOS/EverEvolving" <<LAUNCH
#!/bin/bash
exec "$VENV/bin/python" -m ee_agent.desktop
LAUNCH
  chmod +x "$APP/Contents/MacOS/EverEvolving"
  if command -v iconutil >/dev/null 2>&1 && command -v sips >/dev/null 2>&1; then
    ICONSET="$DATA/everevolving.iconset"
    rm -rf "$ICONSET"; mkdir -p "$ICONSET"
    for size in 16 32 128 256; do
      sips -z $size $size "$ICON_PNG" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
      double=$((size * 2))
      sips -z $double $double "$ICON_PNG" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
    done
    iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/everevolving.icns" && rm -rf "$ICONSET"
  fi
  cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>com.everevolving.tradingagent</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>EverEvolving</string>
  <key>CFBundleIconFile</key><string>everevolving</string>
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST
  ln -sfn "$APP" "$HOME/Desktop/$APP_NAME.app"
  say "$APP"
  say "$HOME/Desktop/$APP_NAME.app"
  LAUNCH_CMD=(open "$APP")
else
  ENTRY="[Desktop Entry]
Type=Application
Name=$APP_NAME
Comment=Describe your strategy, backtest it, and prove it is the same everywhere.
Exec=\"$VENV/bin/python\" -m ee_agent.desktop
Icon=$ICON_PNG
Terminal=false
Categories=Office;Finance;"
  mkdir -p "$HOME/.local/share/applications"
  printf '%s\n' "$ENTRY" > "$HOME/.local/share/applications/everevolving.desktop"
  if [ -d "$HOME/Desktop" ]; then
    printf '%s\n' "$ENTRY" > "$HOME/Desktop/everevolving.desktop"
    chmod +x "$HOME/Desktop/everevolving.desktop"
    command -v gio >/dev/null 2>&1 && gio set "$HOME/Desktop/everevolving.desktop" metadata::trusted true 2>/dev/null || true
  fi
  say "$HOME/.local/share/applications/everevolving.desktop"
  LAUNCH_CMD=("$VENV/bin/python" -m ee_agent.desktop)
fi
printf '{"repo": "%s", "venv": "%s"}\n' "$REPO" "$VENV" > "$DATA/install.json"

step "5 of 5  Opening the app"
if [ "$NO_LAUNCH" = 1 ]; then
  say "Skipped (--no-launch). Open '$APP_NAME' from your Desktop."
else
  ( "${LAUNCH_CMD[@]}" >/dev/null 2>&1 & )
  say "The app window is opening. Next time, open '$APP_NAME' from your Desktop."
fi
printf '\n  Done.\n\n'
