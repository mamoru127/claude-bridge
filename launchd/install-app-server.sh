#!/bin/bash
# ブリッジと Codex アプリが共有する codex app-server を launchd へ登録し、
# 制御ソケットが応答するまで確認する。何度実行してもよい（登録済みなら入れ替える）。
set -euo pipefail

source_dir="$(cd "$(dirname "$0")" && pwd)"
bridge_python="${CLAUDE_BRIDGE_PYTHON:-$(command -v python3)}"
if [ ! -x "$bridge_python" ]; then
  echo "Bridge Python is missing: $bridge_python" >&2
  exit 1
fi
setup_connection() {
  /usr/bin/python3 "$source_dir/install-app-connection.py"
  PYTHONPATH="$source_dir/.." "$bridge_python" -m claude_bridge.app_tools_setup
}
if [ "${1:-}" = "--connection-only" ] && [ "$#" -eq 1 ]; then
  setup_connection
  exit 0
fi
if [ "$#" -ne 0 ]; then
  echo "Usage: $0 [--connection-only]" >&2
  exit 2
fi

label="local.codex-app-server"
source_plist="$(cd "$(dirname "$0")" && pwd)/$label.plist"
source_wrapper="$(cd "$(dirname "$0")" && pwd)/codex-app-server-parent.mjs"
installed_plist="$HOME/Library/LaunchAgents/$label.plist"
installed_wrapper_dir="$HOME/.local/share/claude-bridge"
installed_wrapper="$installed_wrapper_dir/codex-app-server-parent.mjs"
domain="gui/$(id -u)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
socket_path="$codex_home/app-server-control/app-server-control.sock"
app_codex="/Applications/ChatGPT.app/Contents/Resources/codex"
app_node="/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node"

mkdir -p \
  "$HOME/Library/Logs/codex-app-server" \
  "$HOME/Library/LaunchAgents" \
  "$installed_wrapper_dir"
install -m 0644 "$source_wrapper" "$installed_wrapper"
sed -e "s|__HOME__|$HOME|g" "$source_plist" > "$installed_plist"
/usr/libexec/PlistBuddy -c "Set :ProgramArguments:0 $app_node" "$installed_plist"
/usr/libexec/PlistBuddy -c "Set :ProgramArguments:1 $installed_wrapper" "$installed_plist"
/usr/libexec/PlistBuddy -c "Set :ProgramArguments:2 $app_codex" "$installed_plist"

# 登録済みなら一度外す。未登録の場合の失敗は無視する。
launchctl bootout "$domain/$label" 2>/dev/null || true

# 手動で立てた app-server がソケットを掴んだままだと bind に失敗する。
# 掴んでいるのが誰かを示すだけにして、停止は行わない。
if [ -S "$socket_path" ]; then
  echo "既存のソケットがあります: $socket_path"
  lsof -nP "$socket_path" 2>/dev/null || true
fi

launchctl bootstrap "$domain" "$installed_plist"

for _ in $(seq 1 30); do
  if "$app_codex" app-server daemon version >/dev/null 2>&1; then
    "$app_codex" app-server daemon version
    setup_connection
    exit 0
  fi
  sleep 1
done

echo "app-server が応答しません。ログを確認してください: $HOME/Library/Logs/codex-app-server/app-server.log" >&2
exit 1
