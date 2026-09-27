#!/bin/bash
# iOS/Android Remote の新規スレッドを ChatGPT Mac版でバックグラウンド表示する。
set -euo pipefail

label="local.codex-mobile-browser-route"
source_dir="$(cd "$(dirname "$0")" && pwd)"
source_plist="$source_dir/$label.plist"
working_dir="$(cd "$source_dir/.." && pwd)"
python_bin="${CLAUDE_BRIDGE_PYTHON:-$(command -v python3)}"
installed_plist="$HOME/Library/LaunchAgents/$label.plist"
domain="gui/$(id -u)"

mkdir -p \
  "$HOME/Library/Logs/codex-mobile-browser-route" \
  "$HOME/Library/LaunchAgents"
sed -e "s|__PYTHON_BIN__|$python_bin|g" \
    -e "s|__WORKING_DIR__|$working_dir|g" \
    -e "s|__HOME__|$HOME|g" \
    "$source_plist" > "$installed_plist"

launchctl bootout "$domain/$label" 2>/dev/null || true
launchctl bootstrap "$domain" "$installed_plist"
launchctl print "$domain/$label" | grep -E '^\s+(state|pid) = '
