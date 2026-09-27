#!/bin/sh
# Установка LaunchAgent на macOS: ./macos/install.sh <TV_DEVICE_ID>
set -e
cd "$(dirname "$0")/.."
ID="$1"
if [ -z "$ID" ]; then
    echo "usage: $0 <TV_DEVICE_ID>   (узнать: python3 tcl-airplay-proxy.py --find)" >&2
    exit 1
fi
PLIST="$HOME/Library/LaunchAgents/local.tcl-airplay-proxy.plist"
mkdir -p "$HOME/.local/bin" "$HOME/Library/LaunchAgents"
install -m 755 tcl-airplay-proxy.py "$HOME/.local/bin/tcl-airplay-proxy.py"
sed -e "s|__HOME__|$HOME|g" -e "s|__TV_DEVICE_ID__|$ID|g" macos/local.tcl-airplay-proxy.plist > "$PLIST"
launchctl bootout "gui/$(id -u)/local.tcl-airplay-proxy" 2>/dev/null && sleep 2 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed; log: ~/Library/Logs/tcl-airplay-proxy.log"
