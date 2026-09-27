#!/bin/bash
# claude-bridge を launchd のユーザーエージェントとして登録し、起動するまで確認する。
# 何度実行してもよい（登録済みなら入れ替える）。
set -euo pipefail

label="local.claude-bridge"
source_dir="$(cd "$(dirname "$0")" && pwd)"
source_plist="$source_dir/$label.plist"
working_dir="$(cd "$source_dir/.." && pwd)"
python_bin="${CLAUDE_BRIDGE_PYTHON:-$(command -v python3)}"
installed_plist="$HOME/Library/LaunchAgents/$label.plist"
domain="gui/$(id -u)"
health_url="http://127.0.0.1:8787/health"
log_dir="$HOME/Library/Logs/claude-bridge"

# 自分がブリッジの子孫（ブリッジ経由で動く Claude CLI のシェル）かどうか。
# bootout はブリッジのプロセスグループごと SIGKILL するため、子孫から直接実行すると
# bootstrap へ進む前に自分が死に、登録解除だけが残ってブリッジが二度と上がらない。
running_inside_bridge() {
  local bridge_pid pid
  bridge_pid="$(launchctl print "$domain/$label" 2>/dev/null | awk '/^[[:space:]]+pid = /{print $3}')"
  [ -n "$bridge_pid" ] || return 1
  pid=$$
  while [ "$pid" -gt 1 ]; do
    [ "$pid" = "$bridge_pid" ] && return 0
    pid="$(ps -o ppid= -p "$pid" | tr -d ' ')"
    [ -n "$pid" ] || return 1
  done
  return 1
}

mkdir -p "$log_dir" "$HOME/Library/LaunchAgents"

if [ "${1:-}" != "--detached" ] && running_inside_bridge; then
  # set -m でジョブ制御を有効にすると、& で起動した子が新しいプロセスグループになり、
  # bootout の巻き添えを免れる。切り離した側が登録を完了させるので、自分は即座に戻る。
  (set -m; nohup "$0" --detached < /dev/null >> "$log_dir/install.log" 2>&1 &)
  echo "ブリッジ経由で実行されているため、再起動を切り離しました。この接続は切れ、Codex が約 60 秒後に再接続します。結果: $log_dir/install.log"
  exit 0
fi

if [ "${1:-}" = "--detached" ]; then
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') detached reinstall ==="
  # 呼び出し元が上のメッセージを返し終えるまで待ってから止める。
  sleep 1
fi

sed -e "s|__PYTHON_BIN__|$python_bin|g" \
    -e "s|__WORKING_DIR__|$working_dir|g" \
    -e "s|__HOME__|$HOME|g" \
    "$source_plist" > "$installed_plist"

# 登録済みなら一度外す。未登録の場合の失敗は無視する。
launchctl bootout "$domain/$label" 2>/dev/null || true

# 手動起動したブリッジが 8787 を掴んだままだと bind に失敗するため、先に止める。
for pid in $(lsof -ti tcp:8787 -sTCP:LISTEN || true); do
  echo "stopping existing listener pid=$pid"
  kill "$pid" 2>/dev/null || true
done

launchctl bootstrap "$domain" "$installed_plist"

for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 "$health_url" >/dev/null 2>&1; then
    echo "ready: $(launchctl print "$domain/$label" | grep -E '^\s+pid = ')"
    exit 0
  fi
  sleep 1
done

echo "健康チェックに失敗しました。ログを確認してください: $log_dir/bridge.log" >&2
exit 1
