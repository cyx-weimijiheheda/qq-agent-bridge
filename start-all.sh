#!/usr/bin/env bash
# 一键启动 NapCat + QQ 机器人（本机辅助脚本，不属于程序本体）
#
#   ./start-all.sh                启动 NapCat 与机器人
#   NAPCAT_DIR=/opt/NapCat ./start-all.sh   指定 NapCat 目录
#   ./start-all.sh --no-napcat    只启动机器人（NapCat 已在别处跑着时）
#   ./start-all.sh --no-qr        需要扫码时不自动打开二维码窗口
#
# 注意：NapCat 必须在它自己的目录里启动 —— AppRun 把当前目录当作配置目录，
# 换个目录启动就等于换了一份空配置，表现为 6099 能开但 3001 永远不监听。
set -euo pipefail

cd "$(dirname "$0")"

NAPCAT_DIR="${NAPCAT_DIR:-$HOME/NapCat}"
QR_FILE="$NAPCAT_DIR/cache/qrcode.png"
QR_VIEWER_DIR="$HOME/.cache/napcat-qr-viewer"
NAPCAT_SKIP=0
QR_SKIP=0
for arg in "$@"; do
  case "$arg" in
    --no-napcat) NAPCAT_SKIP=1 ;;
    --no-qr) QR_SKIP=1 ;;
  esac
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
onebot_up() { ss -tln 2>/dev/null | grep -q ':3001 '; }

# NapCat 只在需要登录时才写 qrcode.png；窗口每 4 秒重载，出码后会自动跟上
qr_available() { [ -f "$QR_FILE" ]; }

ensure_qr_viewer() {
  mkdir -p "$QR_VIEWER_DIR"
  ln -sfn "$QR_FILE" "$QR_VIEWER_DIR/qrcode.png"
  cat > "$QR_VIEWER_DIR/index.html" <<'HTML'
<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>NapCat 登录二维码</title>
<meta http-equiv="refresh" content="4">
<style>
  body { margin:0; min-height:100vh; display:flex; flex-direction:column;
         align-items:center; justify-content:center; gap:1rem;
         font-family: system-ui,"Noto Sans CJK SC",sans-serif;
         background:#14131a; color:#e8e4ee; }
  h1 { font-size:1.2rem; margin:0; }
  .card { background:#fff; padding:1rem; border-radius:12px; box-shadow:0 6px 30px rgba(0,0,0,.55); }
  img { display:block; width:320px; height:320px; image-rendering:pixelated; }
  .hint { font-size:.875rem; opacity:.75; text-align:center; line-height:1.7; }
  .stamp { font-variant-numeric:tabular-nums; opacity:.45; font-size:.75rem; }
</style>
<h1>NapCat 登录二维码</h1>
<div class="card"><img id="qr" alt="登录二维码"></div>
<p class="hint">用手机 QQ 扫码登录<br>每 4 秒自动刷新，跟随 NapCat 重新出码</p>
<p class="stamp" id="stamp"></p>
<script>
  var t = Date.now();
  document.getElementById('qr').src = 'qrcode.png?t=' + t;
  document.getElementById('stamp').textContent = '最近刷新 ' + new Date(t).toLocaleTimeString('zh-CN');
</script>
</html>
HTML
}

open_qr() {
  if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    echo "    当前没有图形环境，请手动查看：$QR_FILE"
    return 0
  fi
  if ! command -v xdg-open >/dev/null 2>&1; then
    echo "    找不到 xdg-open，请手动查看：$QR_FILE"
    return 0
  fi
  ensure_qr_viewer
  setsid xdg-open "$QR_VIEWER_DIR/index.html" >/dev/null 2>&1 &
  echo "    已打开扫码窗口（每 4 秒自动刷新）"
}

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

# ---- 需要扫码时自动弹窗 ----
if [ "$NAPCAT_SKIP" = "0" ]; then
  # 已登录时 3001 会自己起来；等一会儿仍没起来，多半是在等扫码。
  for _ in $(seq 1 6); do
    onebot_up && break
    sleep 1
  done
  if onebot_up; then
    echo "==> OneBot 已就绪（无需扫码）"
  else
    echo "==> OneBot 未就绪，可能需要在手机 QQ 上扫码"
    for _ in $(seq 1 10); do
      qr_available && break
      sleep 1
    done
    if qr_available; then
      if [ "$QR_SKIP" = "1" ]; then
        echo "    二维码：$QR_FILE（--no-qr，未自动打开）"
      else
        open_qr
      fi
    else
      echo "    暂未生成二维码，稍后查看：$QR_FILE"
    fi
  fi
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
