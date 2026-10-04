#!/bin/bash
# Codex アプリと共有 app-server の間に中継を入れる。作業中の Claude のスレッドへアプリから
# 送った指示で、Claude の作業をその場で区切れるようになる。
# 中継が起動してから、アプリの接続先を中継へ切り替える。反映にはアプリの再起動が要る。
# 元に戻すには --uninstall を付けて実行する。
set -euo pipefail

label="local.claude-bridge-relay"
source_dir="$(cd "$(dirname "$0")" && pwd)"
source_plist="$source_dir/$label.plist"
working_dir="$(cd "$source_dir/.." && pwd)"
default_python="$working_dir/.venv/bin/python"
[ -x "$default_python" ] || default_python="$(command -v python3)"
python_bin="${CLAUDE_BRIDGE_PYTHON:-$default_python}"
installed_plist="$HOME/Library/LaunchAgents/$label.plist"
domain="gui/$(id -u)"
relay_socket="$HOME/.claude-bridge/app-relay.sock"
app_server_socket="${CODEX_HOME:-$HOME/.codex}/app-server-control/app-server-control.sock"

if [ "${1:-}" = "--uninstall" ]; then
  # 先にアプリの接続先を共有 app-server へ戻してから中継を止める。
  rm -f "$installed_plist"
  /usr/bin/python3 "$source_dir/install-app-connection.py"
  launchctl bootout "$domain/$label" 2>/dev/null || true
  echo "中継を外しました。ChatGPT アプリを再起動すると、共有 app-server へ直接繋ぎます。"
  exit 0
fi

if [ ! -S "$app_server_socket" ]; then
  echo "共有 app-server のソケットが見つかりません: $app_server_socket" >&2
  exit 1
fi

mkdir -p "$HOME/Library/Logs/claude-bridge" "$HOME/Library/LaunchAgents" "$(dirname "$relay_socket")"
chmod 700 "$(dirname "$relay_socket")"
sed -e "s|__PYTHON_BIN__|$python_bin|g" \
    -e "s|__WORKING_DIR__|$working_dir|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__RELAY_SOCKET__|$relay_socket|g" \
    -e "s|__APP_SERVER_SOCKET__|$app_server_socket|g" \
    "$source_plist" > "$installed_plist"

launchctl bootout "$domain/$label" 2>/dev/null || true
rm -f "$relay_socket"
launchctl bootstrap "$domain" "$installed_plist"

# 中継が待ち受けを始めるまで、アプリの接続先は切り替えない。
for _ in $(seq 1 50); do
  [ -S "$relay_socket" ] && break
  sleep 0.2
done
if [ ! -S "$relay_socket" ]; then
  rm -f "$installed_plist"
  launchctl bootout "$domain/$label" 2>/dev/null || true
  echo "中継が起動しませんでした。アプリの接続先は変えていません: $HOME/Library/Logs/claude-bridge/relay.log" >&2
  exit 1
fi

/usr/bin/python3 "$source_dir/install-app-connection.py"
echo "作業中のタスクを止めてから ChatGPT アプリを再起動すると、中継経由の接続に切り替わります。"
