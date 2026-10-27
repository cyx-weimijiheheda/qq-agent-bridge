#!/usr/bin/env bash
# 停止由 start-all.sh 启动的 SnowLuma 与 QQ 机器人。
#
#   ./stop-all.sh            优雅停止
#   ./stop-all.sh --force    5 秒后仍有残留则强杀
set -uo pipefail

cd "$(dirname "$0")"
FORCE=0
for arg in "$@"; do
  [ "$arg" = "--force" ] && FORCE=1
done

echo "==> 发送 SIGTERM"
PIDS=()
for pidfile in data/snowluma.pid data/bridge.pid; do
  [ -f "$pidfile" ] || continue
  pid=$(cat "$pidfile" 2>/dev/null)
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    # start-all.sh 用 setsid 创建独立进程组，终止整组以覆盖 launcher 子进程。
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    echo "    $pidfile（进程组 $pid）"
    PIDS+=("$pid")
  fi
done
if ! pgrep -f '[b]ridge.py' >/dev/null 2>&1; then
  :
elif [ ! -f data/bridge.pid ]; then
  # 兼容通过 start.sh 或其他方式启动的机器人进程。
  pkill -TERM -f '[b]ridge.py' 2>/dev/null || true
  echo "    bridge.py（外部启动进程）"
fi

sleep 5
if [ "$FORCE" = "1" ]; then
  for pid in "${PIDS[@]}"; do
    kill -KILL -- "-$pid" 2>/dev/null || true
  done
fi
rm -f data/snowluma.pid data/bridge.pid

echo
echo "==> 剩余服务端口："
ss -ltn 2>/dev/null | grep -E ':(3001|5099|8080)' | awk '{printf "    %s\n", $4}' || true
ss -ltn 2>/dev/null | grep -qE ':(3001|5099|8080)' || echo "    无（已全部停止）"
