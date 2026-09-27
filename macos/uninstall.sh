#!/bin/sh
launchctl bootout "gui/$(id -u)/local.tcl-airplay-proxy" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/local.tcl-airplay-proxy.plist" "$HOME/.local/bin/tcl-airplay-proxy.py"
echo "removed"
