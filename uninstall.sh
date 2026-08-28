#!/bin/bash
# Retire l'agent et les binaires. Ne touche jamais au vault Obsidian.
set -euo pipefail
LABEL="com.flow2obsidian.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
rm -f "$HOME/.local/bin/flow2obsidian" "$HOME/bin/flow2obsidian"
echo "Agent et commande retirés."
echo "Conservés : ~/.local/share/flow2obsidian (config, état) et ton vault."
echo "Pour tout effacer : rm -rf ~/.local/share/flow2obsidian"
