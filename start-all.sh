#!/usr/bin/env bash
# 本机一键启动 SnowLuma 与 QQ 机器人；SnowLuma 也可单独用 Docker 部署。
#
#   ./start-all.sh
#   SNOWLUMA_DIR=/opt/SnowLuma ./start-all.sh   指定 SnowLuma 解压目录
#   ./start-all.sh --no-snowluma              SnowLuma 已在别处运行时只启动机器人
set -euo pipefail

cd "$(dirname "$0")"
ROOT_DIR="$(pwd)"

SNOWLUMA_DIR="${SNOWLUMA_DIR:-$HOME/SnowLuma}"
SKIP_SNOWLUMA=0
for arg in "$@"; do
  case "$arg" in
    --no-snowluma) SKIP_SNOWLUMA=1 ;;
  esac
done

if [ -x .venv/bin/python ]; then
  PYTHON=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  echo "找不到 Python" >&2
  exit 1
fi

running() { pgrep -f "$1" >/dev/null 2>&1; }
onebot_up() { ss -ltn 2>/dev/null | grep -q ':3001 '; }

# ---- SnowLuma ----
if [ "$SKIP_SNOWLUMA" = "1" ]; then
  echo "==> 跳过 SnowLuma（--no-snowluma）"
elif onebot_up; then
  echo "==> OneBot WebSocket 已监听 3001，沿用现有框架服务"
elif [ -x "$SNOWLUMA_DIR/launcher.sh" ]; then
  echo "==> 启动 SnowLuma（$SNOWLUMA_DIR）"
  mkdir -p data
  (
    cd "$SNOWLUMA_DIR"
    setsid nohup ./launcher.sh >> "$SNOWLUMA_DIR/snowluma-console.log" 2>&1 < /dev/null &
    echo "$!" > "$ROOT_DIR/data/snowluma.pid"
  )
  echo "    日志：$SNOWLUMA_DIR/snowluma-console.log"
elif [ -x "$SNOWLUMA_DIR/launcher.bat" ]; then
  echo "SnowLuma 目录看起来是 Windows 发行包，请在 Windows 上运行 launcher.bat。" >&2
  exit 1
else
  echo "找不到 $SNOWLUMA_DIR/launcher.sh。请按 SnowLuma 官方发行包指南安装。" >&2
  echo "指南：https://snowluma.github.io/en/docs/guide/quickstart" >&2
  exit 1
fi

# ---- QQ 机器人 ----
if running "[b]ridge.py"; then
  echo "==> 机器人已在运行"
else
  echo "==> 启动机器人"
  mkdir -p data
  setsid nohup "$PYTHON" bridge.py >> data/console.log 2>&1 < /dev/null &
  echo "$!" > data/bridge.pid
  sleep 2
fi

echo
echo "==> 当前监听："
ss -ltn 2>/dev/null | grep -E ':(3001|5099|8080)' | awk '{printf "    %s\n", $4}' || true
echo "    SnowLuma WebUI：http://127.0.0.1:5099（首次登录/扫码请在此完成）"
if grep -q '"web_enabled": true' config.json 2>/dev/null; then
  PORT=$(grep -oE '"web_port": *[0-9]+' config.json | grep -oE '[0-9]+' || echo 8080)
  echo "    Web 工作台：http://127.0.0.1:$PORT"
fi
echo "    机器人日志：data/bridge.log    停止：./stop-all.sh"
