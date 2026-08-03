#!/bin/bash
# ブリッジと Codex アプリが共有する codex app-server を launchd へ登録し、
# 制御ソケットが応答するまで確認する。何度実行してもよい（登録済みなら入れ替える）。
set -euo pipefail

label="local.codex-app-server"
source_plist="$(cd "$(dirname "$0")" && pwd)/$label.plist"
installed_plist="$HOME/Library/LaunchAgents/$label.plist"
domain="gui/$(id -u)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
socket_path="$codex_home/app-server-control/app-server-control.sock"
app_codex="/Applications/ChatGPT.app/Contents/Resources/codex"

mkdir -p "$HOME/Library/Logs/codex-app-server" "$HOME/Library/LaunchAgents"

# plist のパスは環境ごとに違うため、登録時に埋める。
sed -e "s|__HOME__|$HOME|g" "$source_plist" > "$installed_plist"

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
    exit 0
  fi
  sleep 1
done

echo "app-server が応答しません。ログを確認してください: $HOME/Library/Logs/codex-app-server/app-server.log" >&2
exit 1
