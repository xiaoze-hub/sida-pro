#!/usr/bin/env bash
# ============================================================
# 预测引擎(8010) 部署脚本 —— **裸 venv 进程 + systemd**(非容器)
#
# 形态定稿(2026-10-10): 预测引擎历来**不是容器化部署** ——
#   docker-compose.yml 里的 `forecast:8010` 服务只是早期规划描述, ACR/ghcr 上
#   从未发布过 `*-forecast` 镜像(生产实测 `docker manifest inspect` 不存在该仓库)。
#   生产真实形态 = 主机 venv 里 `python3 forecast_server.py`, 由 systemd 单元
#   `panwatch-forecast.service` 常驻 + 开机自启; 历史库 `~/.panwatch_forecast.db`。
#   详见 deploy/FORECAST_ENGINE_DEPLOY.md。
#
# 为什么要绑 0.0.0.0: 主服务(panwatch)在容器里, 它经 `_detect_engine_url()` 从
#   容器默认网关推出引擎地址(如 http://172.19.0.1:8010, 即宿主机在 compose 网络上的
#   IP)。裸进程若只听 127.0.0.1, 容器侧连接必然被拒。故 FORECAST_HOST 默认 0.0.0.0
#   (与 `-p 8000:8000` 同口径; 生产主机 ufw inactive, 与主服务暴露面一致)。
#
# 幂等: 重复执行 = 同步代码 → 复用/建 venv → 装依赖 → 装 unit → 拉起 → 健康检查。
#
# 用法(在目标主机上以 root 运行):
#   sudo bash deploy/deploy_forecast_engine.sh
# 主要可覆盖变量:
#   FORECAST_SOURCE_DIR    仓库根(含 forecast_server.py / forecast_lib/), 默认本脚本上级
#   FORECAST_INSTALL_DIR   安装目录, 默认 /opt/panwatch-forecast
#   FORECAST_VENV_DIR      venv 目录, 默认 $FORECAST_INSTALL_DIR/venv
#   FORECAST_MAIN_API_URL  主服务地址, 默认 http://127.0.0.1:8000(容器 8000 已发布到宿主机)
#   FORECAST_HOST          引擎绑定地址, 默认 0.0.0.0(容器可达所必需, 见上)
#   FORECAST_DB_PATH       历史库路径, 默认 $HOME/.panwatch_forecast.db
#   FORECAST_SKIP_DEPS=1   跳过依赖安装
#   FORECAST_DEPLOY_STUB=1 干跑(不建 venv/不装 unit/不拉起, 供 stub 测试)
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="${FORECAST_SOURCE_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
INSTALL_DIR="${FORECAST_INSTALL_DIR:-/opt/panwatch-forecast}"
VENV_DIR="${FORECAST_VENV_DIR:-$INSTALL_DIR/venv}"
ENV_FILE="${FORECAST_ENV_FILE:-$INSTALL_DIR/forecast.env}"
UNIT_DIR="${FORECAST_SYSTEMD_DIR:-/etc/systemd/system}"
UNIT_NAME="${FORECAST_UNIT_NAME:-panwatch-forecast.service}"
SERVICE="${UNIT_NAME%.service}"
PYTHON="${FORECAST_PYTHON:-python3}"
MAIN_API_URL="${FORECAST_MAIN_API_URL:-http://127.0.0.1:8000}"
BIND_HOST="${FORECAST_HOST:-0.0.0.0}"
DB_PATH="${FORECAST_DB_PATH:-$HOME/.panwatch_forecast.db}"
PIP_MIRROR="${FORECAST_PIP_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"
LOG_FILE="${FORECAST_LOG_FILE:-/var/log/panwatch-forecast.log}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
PORT="${FORECAST_PORT:-8010}"
STUB="${FORECAST_DEPLOY_STUB:-0}"
SKIP_DEPS="${FORECAST_SKIP_DEPS:-0}"

log() { echo "[forecast-deploy] $*"; }

# ── ① 源码就位 ──────────────────────────────────────────────
if [ ! -f "$SOURCE_DIR/forecast_server.py" ] || [ ! -d "$SOURCE_DIR/forecast_lib" ]; then
  echo "[forecast-deploy] ❌ 源码缺失: $SOURCE_DIR 需含 forecast_server.py + forecast_lib/" >&2
  exit 1
fi
mkdir -p "$INSTALL_DIR"
cp -f "$SOURCE_DIR/forecast_server.py" "$INSTALL_DIR/forecast_server.py"
rm -rf "$INSTALL_DIR/forecast_lib"
mkdir -p "$INSTALL_DIR/forecast_lib"
cp -f "$SOURCE_DIR"/forecast_lib/*.py "$INSTALL_DIR/forecast_lib/"
log "代码已同步 → $INSTALL_DIR (forecast_server.py + forecast_lib/)"

if [ "$STUB" != "1" ]; then
  # ── ② venv(没有就建; 建完仍无 pip 则 ensurepip 兜底) ──────
  if [ ! -x "$VENV_DIR/bin/python3" ]; then
    log "创建 venv: $VENV_DIR"
    $PYTHON -m venv "$VENV_DIR" 2>&1 | tail -2
  fi
  if [ ! -x "$VENV_DIR/bin/pip" ]; then
    log "venv 缺 pip, 尝试 ensurepip"
    "$VENV_DIR/bin/python3" -m ensurepip --upgrade 2>&1 | tail -2 || true
  fi
  if [ ! -x "$VENV_DIR/bin/pip" ]; then
    echo "[forecast-deploy] ❌ venv 无 pip(缺 python3-venv?); 请先 apt-get install -y python3-venv" >&2
    exit 1
  fi

  # ── ③ 依赖(懒加载: torch 等缺失时引擎降级, 核心端点仍可用) ──
  if [ "$SKIP_DEPS" != "1" ]; then
    "$VENV_DIR/bin/python3" -m pip install -q --upgrade pip -i "$PIP_MIRROR" 2>&1 | tail -1
    REQ="$SOURCE_DIR/forecast_requirements.txt"
    if [ -f "$REQ" ]; then
      log "安装预测引擎依赖(来源 $REQ)"
      "$VENV_DIR/bin/python3" -m pip install -q -i "$PIP_MIRROR" -r "$REQ" 2>&1 | tail -3 \
        || log "⚠️ 依赖安装存在失败项(引擎按懒加载降级, /health 与 /referee/stats 仍可用)"
    else
      log "⚠️ 未找到 forecast_requirements.txt, 跳过依赖安装"
    fi
  fi
fi

# ── ④ env 文件(幂等覆盖; 单一来源) ──────────────────────────
mkdir -p "$(dirname "$ENV_FILE")"
cat > "$ENV_FILE" <<ENVEOF
# 由 deploy/deploy_forecast_engine.sh 生成 —— 重跑脚本会覆盖, 勿手改。
# 引擎经主服务(8000)拿资金流/裁判配置; 历史库落 FORECAST_DB_PATH。
SIDA_MAIN_API_URL=$MAIN_API_URL
FORECAST_HOST=$BIND_HOST
FORECAST_DB_PATH=$DB_PATH
TZ=Asia/Shanghai
PYTHONUNBUFFERED=1
ENVEOF
chmod 600 "$ENV_FILE"
log "env 已写: $ENV_FILE (HOST=$BIND_HOST DB=$DB_PATH)"

# ── ⑤ systemd unit ─────────────────────────────────────────
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/$UNIT_NAME" <<UNITEOF
[Unit]
Description=PanWatch A-Share Forecast Engine (Kronos + XGBoost + Chronos-Bolt + TimesFM + LinearReg)
Documentation=file:$INSTALL_DIR/forecast_server.py
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_DIR/bin/python3 $INSTALL_DIR/forecast_server.py
Restart=always
RestartSec=5
# 预测引擎内存占用大(Kronos ~1GB), 给足上限与慢启动时间
MemoryMax=4G
TimeoutStartSec=300
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=multi-user.target
UNITEOF
log "unit 已写: $UNIT_DIR/$UNIT_NAME"

# ── ⑥ 拉起 ─────────────────────────────────────────────────
if [ "$STUB" = "1" ]; then
  log "[stub] $SYSTEMCTL daemon-reload"
  log "[stub] $SYSTEMCTL enable --now $SERVICE"
  log "[stub] $SYSTEMCTL restart $SERVICE"
  log "[stub] 健康检查跳过"
  echo "FORECAST_DEPLOY_OK (stub)"
  exit 0
fi

if ! command -v "$SYSTEMCTL" >/dev/null 2>&1; then
  echo "[forecast-deploy] ❌ 找不到 $SYSTEMCTL(WSL 未开 systemd? 见 deploy/FORECAST_ENGINE_DEPLOY.md 的 nohup 兜底)" >&2
  exit 1
fi
$SYSTEMCTL daemon-reload
$SYSTEMCTL enable --now "$SERVICE" || { echo "[forecast-deploy] ❌ enable/start $SERVICE 失败" >&2; exit 1; }
$SYSTEMCTL restart "$SERVICE"

# ── ⑦ 健康检查(引擎慢启动: Kronos 加载 ~25-60s) ────────────
ok=0
for i in $(seq 1 30); do
  if "$VENV_DIR/bin/python3" -c "import urllib.request,json;json.load(urllib.request.urlopen('http://127.0.0.1:$PORT/health', timeout=5))" 2>/dev/null; then
    ok=1; log "✅ 引擎健康(第 ${i} 次探测): http://127.0.0.1:$PORT/health"; break
  fi
  sleep 4
done
if [ "$ok" = "1" ]; then
  echo "FORECAST_DEPLOY_OK"
  exit 0
fi
echo "[forecast-deploy] ❌ 引擎 30 次探测未通过; 日志尾:" >&2
tail -20 "$LOG_FILE" 2>/dev/null || true
exit 1
