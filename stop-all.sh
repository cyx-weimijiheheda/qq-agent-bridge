#!/usr/bin/env bash
# 停止 NapCat 与机器人（本机辅助脚本，不属于程序本体）
#
#   ./stop-all.sh            优雅停止（SIGTERM，NapCat 有机会保存登录态与配置）
#   ./stop-all.sh --force    5 秒后仍有残留则强杀
set -uo pipefail

cd "$(dirname "$0")"

FORCE=0
for arg in "$@"; do
  [ "$arg" = "--force" ] && FORCE=1
done

# 方括号写法避免 pgrep 匹配到脚本自身的命令行
PATTERNS=("[b]ridge.py" "[N]apCat.AppImage" "[.]mount_NapCat" "[x]vfb-run" "[h]ttp.server 8321")

echo "==> 发送 SIGTERM"
for pat in "${PATTERNS[@]}"; do
  count=$(pgrep -f "$pat" 2>/dev/null | wc -l | tr -d ' ')
  if [ "$count" -gt 0 ]; then
    pkill -TERM -f "$pat" 2>/dev/null || true
    echo "    $pat  （$count 个）"
  fi
done

sleep 5

left=$(ps -eo args --no-headers 2>/dev/null \
  | grep -E "[b]ridge\.py|[N]apCat\.AppImage|[.]mount_NapCat|[x]vfb-run" \
  | wc -l | tr -d ' ')

if [ "$left" -gt 0 ]; then
  if [ "$FORCE" = "1" ]; then
    echo "==> 仍有 $left 个进程，强制结束"
    for pat in "${PATTERNS[@]}"; do pkill -KILL -f "$pat" 2>/dev/null || true; done
    sleep 1
  else
    echo "!! 仍有 $left 个进程未退出；需要时用 ./stop-all.sh --force" >&2
  fi
fi

echo
echo "==> 剩余端口："
ss -tln 2>/dev/null | grep -E ':(3001|6099|8080|8321)' | awk '{printf "    %s\n", $4}' || true
ss -tln 2>/dev/null | grep -qE ':(3001|6099|8080|8321)' || echo "    无（已全部停止）"
