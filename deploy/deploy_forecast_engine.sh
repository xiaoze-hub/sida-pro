#!/usr/bin/env bash
# ============================================================
# 预测引擎(8010) 部署脚本 —— **裸 venv 进程 + systemd**(非容器)
#
# 形态定稿(2026-10-10): 预测引擎历来**不是容器化部署** ——
#   docker-compose.yml 里的 `forecast:8010` 服务只是早期规划描述, ACR/ghcr 上
#   从未发布过 `*-forecast` 镜像(生产实测 `docker manifest inspect` 不存在该仓库)。
#   生产真实形态 = 主机 venv 里 `python3 forecast_server.py`, 由 systemd 单元
#   `sida-forecast.service` 常驻 + 开机自启; 历史库 `~/.sida_forecast.db`。
#   详见 deploy/FORECAST_ENGINE_DEPLOY.md。
#
# 为什么要绑 0.0.0.0: 主服务(sida)在容器里, 它经 `_detect_engine_url()` 从
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
#   FORECAST_INSTALL_DIR   安装目录, 默认 /opt/sida-forecast
#   FORECAST_VENV_DIR      venv 目录, 默认 $FORECAST_INSTALL_DIR/venv
#   FORECAST_MAIN_API_URL  主服务地址, 默认 http://127.0.0.1:8000(容器 8000 已发布到宿主机)
#   FORECAST_HOST          引擎绑定地址, 默认 0.0.0.0(容器可达所必需, 见上)
#   FORECAST_DB_PATH       历史库路径, 默认 $HOME/.sida_forecast.db
#   FORECAST_SKIP_DEPS=1   跳过依赖安装
#   FORECAST_DEPLOY_STUB=1 干跑(不建 venv/不装 unit/不拉起, 供 stub 测试)
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="${FORECAST_SOURCE_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
INSTALL_DIR="${FORECAST_INSTALL_DIR:-/opt/sida-forecast}"
VENV_DIR="${FORECAST_VENV_DIR:-$INSTALL_DIR/venv}"
ENV_FILE="${FORECAST_ENV_FILE:-$INSTALL_DIR/forecast.env}"
UNIT_DIR="${FORECAST_SYSTEMD_DIR:-/etc/systemd/system}"
UNIT_NAME="${FORECAST_UNIT_NAME:-sida-forecast.service}"
SERVICE="${UNIT_NAME%.service}"
PYTHON="${FORECAST_PYTHON:-python3}"
MAIN_API_URL="${FORECAST_MAIN_API_URL:-http://127.0.0.1:8000}"
BIND_HOST="${FORECAST_HOST:-0.0.0.0}"
DB_PATH="${FORECAST_DB_PATH:-$HOME/.sida_forecast.db}"
PIP_MIRROR="${FORECAST_PIP_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"
LOG_FILE="${FORECAST_LOG_FILE:-/var/log/sida-forecast.log}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
PORT="${FORECAST_PORT:-8010}"
STUB="${FORECAST_DEPLOY_STUB:-0}"
SKIP_DEPS="${FORECAST_SKIP_DEPS:-0}"
# Kronos 主模型(2026-10-21 补): 源码随仓库 vendored 在 kronos_src/, 权重走 HF Hub。
#   引擎以 `~/Kronos` 为源码根(forecast_lib/forecast_models.py: KRONOS_ROOT), import 名 `model`。
#   旧脚本完全不装 Kronos → 生产 kronos_ready=false, 预测主模型静默缺席。这里做成幂等安装。
KRONOS_SRC_DIR="${FORECAST_KRONOS_SRC:-$SOURCE_DIR/kronos_src}"
KRONOS_HOME="${FORECAST_KRONOS_HOME:-/root/Kronos}"
KRONOS_HF_REPOS="${FORECAST_KRONOS_HF_REPOS:-NeoQuasar/Kronos-small NeoQuasar/Kronos-Tokenizer-base}"
SKIP_KRONOS="${FORECAST_SKIP_KRONOS:-0}"

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

# ── ③b Kronos 主模型源码 + 权重(幂等) ──────────────────────
# 为什么必须在这装: forecast_lib/forecast_models.py 用 `~/Kronos` 作源码根、`import model`
#   拿 KronosPredictor。目录不在 → ModuleNotFoundError → kronos_ready=false, 5 票模型里
#   主模型静默缺席(2026-10-21 生产实锤)。源码全量 vendored 在 kronos_src/(只有 ~60KB py),
#   所以源码随脚本走; 权重走 HF Hub(snapshot_download, 幂等命中缓存)。
if [ "$SKIP_KRONOS" = "1" ]; then
  log "FORECAST_SKIP_KRONOS=1, 跳过 Kronos 安装"
elif [ ! -d "$KRONOS_SRC_DIR/model" ]; then
  # 源码目录缺失有两种事实，必须分开说：目标已就绪(沿用现有) ≠ 模型会缺席。
  if [ -d "$KRONOS_HOME/model" ]; then
    log "ℹ️ 无可安装的 Kronos 源码 $KRONOS_SRC_DIR/model, 但目标 $KRONOS_HOME/model 已存在, 跳过安装(沿用现有源码)"
  else
    log "⚠️ Kronos 源码缺失($KRONOS_SRC_DIR/model 与 $KRONOS_HOME/model 都不存在): 引擎将以 kronos_ready=false 运行(5 票模型缺主模型), 需先放置源码再重跑"
  fi
else
  if [ "$STUB" = "1" ]; then
    log "[stub] 安装 Kronos 源码 $KRONOS_SRC_DIR/model → $KRONOS_HOME/model"
    log "[stub] 预下载权重: $KRONOS_HF_REPOS"
    log "[stub] 校验 $VENV_DIR/bin/python3 -c 'import model'"
  else
    mkdir -p "$KRONOS_HOME"
    rm -rf "$KRONOS_HOME/model"
    cp -r "$KRONOS_SRC_DIR/model" "$KRONOS_HOME/model"
    for _f in __init__.py kronos.py module.py; do
      [ -s "$KRONOS_HOME/model/$_f" ] || { echo "[forecast-deploy] ❌ Kronos 源码不完整: 缺 model/$_f" >&2; exit 1; }
    done
    for _f in README.md LICENSE requirements.txt; do
      [ -f "$KRONOS_SRC_DIR/$_f" ] && cp -f "$KRONOS_SRC_DIR/$_f" "$KRONOS_HOME/$_f"
    done
    log "Kronos 源码已就位 → $KRONOS_HOME/model"

    # 权重预下载: 用 venv python 进程内调用, 避开 `huggingface-cli download` 在部分版本的
    #   交互式 prompt(会把 systemd/CI 语境卡死)。失败不阻断部署, 首次推理仍可在线下载。
    HF_HUB_DISABLE_PROGRESS_BARS=1 "$VENV_DIR/bin/python3" - "$KRONOS_HF_REPOS" <<'PYEOF' 2>&1 | tail -4 \
      || log "⚠️ Kronos 权重预下载失败(首次 /predict 会在线上自动下载, /health 仍会如实报 kronos_error)"
import sys
from huggingface_hub import snapshot_download
for repo in sys.argv[1].split():
    print("cached", repo, "->", snapshot_download(repo_id=repo))
PYEOF

    if KRONOS_CHECK_PATH="$KRONOS_HOME" "$VENV_DIR/bin/python3" -c \
        "import sys,os; sys.path.insert(0, os.environ.get('KRONOS_CHECK_PATH','/root/Kronos')); import model; print('[forecast-deploy] import model OK ->', os.path.dirname(model.__file__))"; then
      log "Kronos import 校验通过(运行时 KRONOS_ROOT=~/Kronos 由其自身 sys.path 注入, 见 forecast_models.py)"
    else
      echo "[forecast-deploy] ❌ Kronos 装完仍 import 失败; /health 会显示 kronos_ready=false, 请查上方报错" >&2
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
Description=SIDA A-Share Forecast Engine (Kronos + XGBoost + Chronos-Bolt + TimesFM + LinearReg)
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
kronos_ready=0
for i in $(seq 1 30); do
  _body=$("$VENV_DIR/bin/python3" -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:$PORT/health', timeout=5).read().decode())" 2>/dev/null) || _body=""
  if [ -n "$_body" ]; then
    ok=1
    log "✅ 引擎健康(第 ${i} 次探测): http://127.0.0.1:$PORT/health → $_body"
    # /health 永不 500(模型缺失只在 body 里报 kronos_ready=false), 所以 HTTP 通 ≠ 主模型在。
    # 主模型缺失必须显式失败, 否则就是 2026-10-21 那种"部署全绿但 Kronos 缺席"的静默降级。
    case "$_body" in
      *'"kronos_ready": true'*|*'"kronos_ready":true'*) kronos_ready=1; break ;;
    esac
    log "⏳ 引擎已起但 Kronos 未就绪(第 ${i} 次), 继续等模型加载..."
  fi
  sleep 4
done
if [ "$ok" = "1" ] && [ "$kronos_ready" = "1" ]; then
  echo "FORECAST_DEPLOY_OK"
  exit 0
fi
if [ "$ok" = "1" ]; then
  echo "[forecast-deploy] ❌ 引擎 HTTP 通了但 kronos_ready=false(主模型缺席 = 静默降级, 不算部署成功)。" >&2
  echo "    修复: 确认 $KRONOS_HOME/model 存在、权重已下载、依赖 einops/safetensors/huggingface_hub 在 venv 内。" >&2
  echo "    仅想先上其它模型可显式 FORECAST_SKIP_KRONOS=1 重跑(此时 /health 会如实报 kronos_error)。" >&2
  exit 1
fi
echo "[forecast-deploy] ❌ 引擎 30 次探测未通过; 日志尾:" >&2
tail -20 "$LOG_FILE" 2>/dev/null || true
exit 1
