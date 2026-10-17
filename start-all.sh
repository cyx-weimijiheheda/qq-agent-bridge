#!/usr/bin/env bash
# 一键启动 NapCat + QQ 机器人（本机辅助脚本，不属于程序本体）
#
#   ./start-all.sh                启动 NapCat 与机器人
#   NAPCAT_DIR=/opt/NapCat ./start-all.sh   指定 NapCat 目录
#   ./start-all.sh --no-napcat    只启动机器人（NapCat 已在别处跑着时）
#
# 注意：NapCat 必须在它自己的目录里启动 —— AppRun 把当前目录当作配置目录，
# 换个目录启动就等于换了一份空配置，表现为 6099 能开但 3001 永远不监听。
set -euo pipefail

cd "$(dirname "$0")"

NAPCAT_DIR="${NAPCAT_DIR:-$HOME/NapCat}"
NAPCAT_SKIP=0
for arg in "$@"; do
  [ "$arg" = "--no-napcat" ] && NAPCAT_SKIP=1
done

# 优先用项目内的虚拟环境
if [ -x .venv/bin/python ]; then
  PYTHON=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="python3"
else
  echo "找不到 Python" >&2
  exit 1
fi

running() { pgrep -f "$1" >/dev/null 2>&1; }

# ---- NapCat ----
if [ "$NAPCAT_SKIP" = "1" ]; then
  echo "==> 跳过 NapCat（--no-napcat）"
elif running "[N]apCat.AppImage"; then
  echo "==> NapCat 已在运行"
else
  if [ ! -x "$NAPCAT_DIR/NapCat.AppImage" ]; then
    echo "找不到 $NAPCAT_DIR/NapCat.AppImage（可用 NAPCAT_DIR 指定目录）" >&2
    exit 1
  fi
  echo "==> 启动 NapCat（工作目录 $NAPCAT_DIR）"
  (
    cd "$NAPCAT_DIR"
    setsid nohup ./NapCat.AppImage >> "$NAPCAT_DIR/napcat-console.log" 2>&1 < /dev/null &
  )
  sleep 3
  echo "    控制台日志：$NAPCAT_DIR/napcat-console.log"
  echo "    如果 QQ 需要重新登录，扫码图在：$NAPCAT_DIR/cache/qrcode.png"
fi

# ---- 机器人 ----
if running "[b]ridge.py"; then
  echo "==> 机器人已在运行"
else
  echo "==> 启动机器人"
  mkdir -p data
  setsid nohup "$PYTHON" bridge.py >> data/console.log 2>&1 < /dev/null &
  sleep 2
fi

# ---- 状态 ----
echo
echo "==> 当前监听："
ss -tln 2>/dev/null | grep -E ':(3001|6099|8080)' | awk '{printf "    %s\n", $4}' || true
echo
if grep -q '"web_enabled": true' config.json 2>/dev/null; then
  PORT=$(grep -oE '"web_port": *[0-9]+' config.json | grep -oE '[0-9]+' || echo 8080)
  echo "    Web 工作台：http://127.0.0.1:$PORT"
fi
echo "    日志：data/bridge.log    停止：./stop-all.sh"
