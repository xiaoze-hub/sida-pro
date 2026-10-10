#!/usr/bin/env bash
# ============================================================
# 预测引擎(8010) 随发版拉起 —— 送代码到目标主机 + 远端执行部署脚本
#
# 形态: **裸 venv 进程 + systemd**(非容器)。核心安装逻辑在
#   deploy/deploy_forecast_engine.sh(在目标主机上以 root 运行); 本脚本只负责
#   "把仓库快照送到目标主机 + 触发它", 复用主服务发版链的 SSH/WSL 送达惯例。
#
# 与主服务发版链的关系: `~/.hermes/scripts/sida_prod_deploy.sh`(部署 panwatch 容器)
#   末尾**非致命**调用本脚本(`|| echo`), 引擎失败不翻转主服务判定。
#   可用 SIDA_SKIP_FORECAST_DEPLOY=1 关闭。
#
# 用法: SIDA_SSH_PW=<pw> bash deploy/sida_prod_deploy_forecast.sh [tag]
# 环境变量:
#   SIDA_REPO_DIR   本地仓库根(含 forecast_server.py), 默认 /home/ubuntu/sida-pro
#   SIDA_PROD_HOST  目标主机, 默认 TIANXIANG@100.91.30.35
#   SIDA_WIN_DIR    目标 Windows 短路径(scp 落地处), 默认 C:/Users/tianxiang
# 成功: 末尾打印 FORECAST_DEPLOY_OK; 失败非 0。
# ============================================================
set -uo pipefail

TAG="${1:-latest}"
: "${SIDA_SSH_PW:=$(cat "$HOME/.hush/sida_ssh_pw" 2>/dev/null)}"
: "${SIDA_SSH_PW:?缺少 SIDA_SSH_PW: 设环境变量或写入 ~/.hush/sida_ssh_pw(600)}"

REPO_DIR="${SIDA_REPO_DIR:-/home/ubuntu/sida-pro}"
HOST="${SIDA_PROD_HOST:-TIANXIANG@100.91.30.35}"
WIN_DIR="${SIDA_WIN_DIR:-C:/Users/tianxiang}"
# 远端展开目录(送到的 tar 解到这里, deploy_forecast_engine.sh 在此运行)
REMOTE_SRC="${SIDA_FORECAST_REMOTE_SRC:-/opt/panwatch-forecast-src}"
TS="$(date +%Y%m%d%H%M%S)"

if [ ! -f "$REPO_DIR/forecast_server.py" ] || [ ! -d "$REPO_DIR/forecast_lib" ]; then
  echo "❌ 本地仓库缺 forecast_server.py / forecast_lib: $REPO_DIR" >&2
  exit 1
fi

LOCAL_TAR="$(mktemp /tmp/forecast_engine_XXXXXX.tgz)"
B64_FILE="$(mktemp /tmp/forecast_deploy_XXXXXX.b64)"
trap 'rm -f "$LOCAL_TAR" "$B64_FILE"' EXIT

echo "→ 预测引擎随发版拉起 (tag=$TAG)"
tar czf "$LOCAL_TAR" -C "$REPO_DIR" --exclude='__pycache__' --exclude='*.pyc' \
  forecast_server.py forecast_lib forecast_requirements.txt deploy \
  || { echo "❌ 打包失败" >&2; exit 1; }

scp -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
  "$LOCAL_TAR" "$HOST:${WIN_DIR}/forecast_engine_${TS}.tgz" \
  || { echo "❌ scp 送达失败(检查 key 与 $HOST 连通性)" >&2; exit 1; }

# 远端脚本经 scp 送达(不内联进命令行, 避免 Windows 命令行长度上限/重解析)
read -r -d '' REMOTE <<REMOTE_EOF
set -uo pipefail
SRC="$REMOTE_SRC"
rm -rf "\$SRC"; mkdir -p "\$SRC"
tar xzf /mnt/c/Users/tianxiang/forecast_engine_${TS}.tgz -C "\$SRC"
bash "\$SRC/deploy/deploy_forecast_engine.sh"
REMOTE_EOF

printf '%s' "$REMOTE" | base64 -w0 > "$B64_FILE"
scp -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
  "$B64_FILE" "$HOST:${WIN_DIR}/forecast_deploy_${TS}.b64" \
  || { echo "❌ b64 送达失败" >&2; exit 1; }

sshpass -p "$SIDA_SSH_PW" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 "$HOST" \
  "wsl -e bash -lc \"base64 -d /mnt/c/Users/tianxiang/forecast_deploy_${TS}.b64 > /tmp/forecast_deploy.sh && bash /tmp/forecast_deploy.sh\""
rc=$?
[ $rc -ne 0 ] && { echo "❌ 远端预测引擎部署失败 rc=$rc" >&2; exit $rc; }
echo "✅ 预测引擎已拉起"
