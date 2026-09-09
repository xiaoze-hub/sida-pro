#!/bin/bash
# ============================================================
# PanWatch 部署/重建恢复脚本
# 用途: 把 fork 仓库(xiaoze-hub/PanWatch)的自定义改动部署进容器
#       容器 docker rm + 重建后代码会丢(数据库配置在卷里保留),
#       运行本脚本即可恢复全部代码改动。
#
# 用法:
#   ./deploy_panwatch.sh          # 部署代码改动 + 重启
#   ./deploy_panwatch.sh --full   # 完整重建容器(含环境变量)
#   ./deploy_panwatch.sh --list   # 列出部署清单
#
# 前提:
#   - /tmp/PanWatch 是 xiaoze-hub/PanWatch 的 git clone(含全部改动)
#   - 容器名 panwatch, 数据卷 panwatch_data
# ============================================================
set -euo pipefail

REPO_DIR="${PANWATCH_REPO:-/tmp/PanWatch}"
CONTAINER="panwatch"
# P1-14: 与 CI 保持一致, 拉 ACR 版本镜像(默认 latest, 发版传 PANWATCH_VERSION=vX.Y.Z)
ACR="crpi-mte80ai8o78b1429.cn-shanghai.personal.cr.aliyuncs.com/xiaozexwz/xzxwz"
IMAGE="${ACR}:${PANWATCH_VERSION:-latest}"
TOKEN="${WUDAO_MCP_TOKEN:-}"
# DOCKER 可用环境变量覆盖(stub 测试用 DOCKER=echo; 默认 sudo docker)
DOCKER="${DOCKER:-${SUDO:-sudo} docker}"

# 加载配置文件(如果存在): panwatch.env 或 deploy_panwatch.env
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for ENV_FILE in "$SCRIPT_DIR/panwatch.env" "$SCRIPT_DIR/deploy_panwatch.env"; do
  if [ -f "$ENV_FILE" ]; then
    echo "▶ 加载配置: $ENV_FILE"
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
    break
  fi
done

# 从配置(或环境变量)取值
CONTAINER="${PANWATCH_CONTAINER:-$CONTAINER}"
IMAGE="${PANWATCH_IMAGE:-$IMAGE}"
REPO_DIR="${PANWATCH_REPO:-$REPO_DIR}"
TOKEN="${WUDAO_MCP_TOKEN:-$TOKEN}"
AUTH_USERNAME="${AUTH_USERNAME:-admin}"
if [ -z "${AUTH_PASSWORD:-}" ]; then
  AUTH_PASSWORD="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 16)"
  echo "未提供 AUTH_PASSWORD，已生成一次性初始密码: $AUTH_PASSWORD"
fi

# 部署清单: 所有被改动的文件 (相对 REPO_DIR)
FILES=(
  "server.py"
  "src/web/app.py"
  "src/web/api/chat.py"
  "src/web/api/calendar.py"
  "src/web/api/tdx.py"
  "packages/marketdata/src/marketdata/vendors/tdx.py"
  "src/agents/auction_review.py"
  "src/agents/theme_launch_detector.py"
  "src/agents/stock_attribution.py"
  "src/collectors/tdx_collector.py"
  "src/collectors/wudao_mcp_client.py"
  "src/collectors/market_sentiment_collector.py"
  "prompts/premarket_outlook.txt"
  "prompts/daily_report.txt"
  "prompts/report_template.md"
  "strategies/panwatch_strategies.yaml"
  "src/web/api/forecast.py"
  "src/web/api/market.py"
  "src/web/api/reports.py"
  "src/web/api/strategies.py"
  "src/web/api/recommendations.py"
  "src/web/api/discovery.py"
  "src/web/api/settings.py"
  "src/web/api/dashboard.py"
  "src/web/api/quotes.py"
  "src/collectors/discovery_collector.py"
  "src/collectors/wudao_mcp_client.py"
  "src/collectors/market_sentiment_collector.py"
  "src/agents/premarket_outlook.py"
  "src/agents/intraday_monitor.py"
  "src/core/signals/signal_pack.py"
  "src/core/strategy_engine.py"
  "src/core/entry_candidates.py"
  "src/core/sector_filter.py"
  # P2-26 (2026-09-05 28号审计): 热补丁清单补 data_source/(thsdk 凭据链常改)
  "data_source/thsdk_l2.py"
  "forecast_lib/forecast_sentiment.py"
  "deploy/sync_forecast_llm.sh"
  "src/agents/stock_attribution.py"
  "packages/marketdata/src/marketdata/vendors/ftshare.py"
  "packages/marketdata/src/marketdata/vendors/zhitu.py"
  "packages/marketdata/src/marketdata/registry.py"
)

echo "=============================================="
echo " PanWatch 部署脚本"
echo " 仓库: $REPO_DIR"
echo " 容器: $CONTAINER"
echo "=============================================="

if [ ! -d "$REPO_DIR/.git" ]; then
  echo "❌ 仓库不存在: $REPO_DIR (先 git clone https://github.com/xiaoze-hub/PanWatch.git)"
  exit 1
fi

deploy_files() {
  echo "▶ 部署代码文件..."
  for f in "${FILES[@]}"; do
    if [ -f "$REPO_DIR/$f" ]; then
      $DOCKER cp "$REPO_DIR/$f" "$CONTAINER:/app/$f"
      echo "  ✅ $f"
    elif [ -d "$REPO_DIR/$f" ]; then
      # 目录(如 strategies/): 整体 cp 进容器对应路径
      $DOCKER cp "$REPO_DIR/$f" "$CONTAINER:/app/$(dirname "$f")/"
      echo "  ✅ $f/ (目录)"
    else
      echo "  ⚠️ 缺失: $f (跳过)"
    fi
  done
  # 容器内语法校验
  $DOCKER exec "$CONTAINER" python3 -c "
import ast, sys
for f in ['src/agents/auction_review.py','src/agents/theme_launch_detector.py','src/agents/stock_attribution.py','src/collectors/wudao_mcp_client.py','src/collectors/market_sentiment_collector.py','server.py','src/web/api/chat.py','src/web/api/recommendations.py','src/core/strategy_engine.py','src/core/entry_candidates.py']:
    try:
        ast.parse(open('/app/'+f).read())
    except Exception as e:
        print(f'语法错误 {f}: {e}'); sys.exit(1)
print('✅ 容器内语法校验通过')
" || { echo "❌ 语法校验失败"; exit 1; }
  # 前端构建 + 部署: 始终重新 build(构建快 ~7s, 避免重建容器后镜像旧前端覆盖)
  if [ -d "$REPO_DIR/frontend/node_modules/.bin" ]; then
    echo "▶ 重新构建前端..."
    (cd "$REPO_DIR/frontend" && ./node_modules/.bin/vite build 2>&1 | tail -3) || { echo "❌ 前端构建失败"; exit 1; }
    $DOCKER cp "$REPO_DIR/frontend/dist/." "$CONTAINER":/app/static/ && echo "  ✅ 前端已部署到 /app/static/"
  else
    echo "  ⚠️ 前端 node_modules 缺失,跳过构建(需手动 build)"
  fi
}

# ── 0.1 (2026-09-08): 重建前先读现有容器的真实配置(env/挂载/端口/网络/重启/内存),
# 防止"脚本硬编码拓扑 ≠ 生产现实"。2026-09-08 实测生产(WSL2 Docker, 无 compose):
# 挂载是 panwatch-data / panwatch-tck(连字符)且在 panwatch-net 自定义网络(SIDA_DB_URL
# 走 postgres 别名解析), 而本脚本原硬编码 panwatch_data(下划线)+无 --network —— 直接重建
# 会用空卷起一个连不上 PG 的新容器。克隆式重建让拓扑以运行中容器为唯一事实源。
CLONE_ENV=(); CLONE_VOL=(); CLONE_PORT=(); CLONE_NET=""; CLONE_RESTART=""; CLONE_MEM=""

harvest_existing_config() {
  local _c="$1" _line _ty _nm _src _dst _md _cp _hp _ip
  CLONE_ENV=(); CLONE_VOL=(); CLONE_PORT=(); CLONE_NET=""; CLONE_RESTART=""; CLONE_MEM=""
  while IFS= read -r _line; do
    [ -n "$_line" ] && CLONE_ENV+=(-e "$_line")
  done < <($DOCKER inspect "$_c" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null)
  while IFS='|' read -r _ty _nm _src _dst _md; do
    [ -z "${_ty:-}" ] && continue
    if [ "$_ty" = "volume" ]; then
      CLONE_VOL+=(-v "${_nm}:${_dst}")
    else
      local _m=""
      case "${_md:-}" in *ro*) _m=":ro";; esac
      CLONE_VOL+=(-v "${_src}:${_dst}${_m}")
    fi
  done < <($DOCKER inspect "$_c" --format '{{range .Mounts}}{{.Type}}|{{.Name}}|{{.Source}}|{{.Destination}}|{{.Mode}}{{println}}{{end}}' 2>/dev/null)
  while IFS='|' read -r _cp _hp _ip; do
    [ -z "${_cp:-}" ] && continue
    if [ -n "${_ip:-}" ]; then CLONE_PORT+=("${_ip}:${_hp}:${_cp%%/*}")
    else CLONE_PORT+=("${_hp}:${_cp%%/*}"); fi
  done < <($DOCKER inspect "$_c" --format '{{range $p, $b := .HostConfig.PortBindings}}{{if $b}}{{$p}}|{{(index $b 0).HostPort}}|{{(index $b 0).HostIp}}{{end}}{{println}}{{end}}' 2>/dev/null)
  CLONE_NET=$($DOCKER inspect "$_c" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' 2>/dev/null)
  case "$CLONE_NET" in ""|"bridge"|"host"|"none") CLONE_NET="";; esac
  CLONE_RESTART=$($DOCKER inspect "$_c" --format '{{.HostConfig.RestartPolicy.Name}}' 2>/dev/null)
  CLONE_MEM=$($DOCKER inspect "$_c" --format '{{.HostConfig.Memory}}' 2>/dev/null)
  CLONE_SWAP=$($DOCKER inspect "$_c" --format '{{.HostConfig.MemorySwap}}' 2>/dev/null)
  CLONE_NANOCPUS=$($DOCKER inspect "$_c" --format '{{.HostConfig.NanoCpus}}' 2>/dev/null)
}

default_config() {
  # 全新安装兜底(与历史行为对齐, 卷名修正为生产同款连字符):
  local _k
  CLONE_ENV=(-e "TZ=Asia/Shanghai" -e "WEB_HOST=0.0.0.0"
             -e "AUTH_USERNAME=$AUTH_USERNAME" -e "AUTH_PASSWORD=$AUTH_PASSWORD"
             -e "DATA_DIR=/app/data" -e "PANWATCH_TCK_DIR=/app/data/tck")
  [ -n "$TOKEN" ] && CLONE_ENV+=(-e "WUDAO_MCP_TOKEN=$TOKEN")
  [ -n "${TDX_API_KEY:-}" ] && CLONE_ENV+=(-e "TDX_API_KEY=$TDX_API_KEY")
  # P1-15 (2026-09-05): 真实 key 禁止进仓库, 空值 = 不注入(走设置页/环境)
  [ -n "${ALPHAVANTAGE_KEYS:-}" ] && CLONE_ENV+=(-e "ALPHAVANTAGE_KEYS=$ALPHAVANTAGE_KEYS")
  [ -n "${TWELVEDATA_KEYS:-}" ] && CLONE_ENV+=(-e "TWELVEDATA_KEYS=$TWELVEDATA_KEYS")
  # P1-14: 生产三链路透传(缺失则不注入)
  for _k in REDIS_URL SIDA_DB_URL THS_USERNAME THS_PASSWORD THS_MAC TDX_API_KEY ZHITU_TOKEN AUTH_USERNAME; do
    if [ -n "${!_k:-}" ]; then CLONE_ENV+=(-e "$_k=${!_k}"); fi
  done
  CLONE_VOL=(-v "${CONTAINER}-data:/app/data" -v "${CONTAINER}-tck:/app/data/tck")
  if [ -d /home/ubuntu/.hermes ]; then
    CLONE_VOL+=(-v /home/ubuntu/.hermes:/hermes:ro)
    CLONE_ENV+=(-e "HERMES_HOME=/hermes")
  fi
  CLONE_PORT=("8000:8000")
  CLONE_NET=""
  CLONE_RESTART="unless-stopped"
  CLONE_MEM="1073741824"
}

compose_run_args() {
  # $1=容器名  $2=宿主机端口(传空 = 继承克隆到的原端口映射)
  local _name="$1" _port="$2" _p
  RUN_ARGS=(--name "$_name")
  if [ -n "$_port" ]; then
    RUN_ARGS+=(-p "${_port}:8000")
  elif [ "${#CLONE_PORT[@]}" -gt 0 ]; then
    for _p in "${CLONE_PORT[@]}"; do RUN_ARGS+=(-p "$_p"); done
  fi
  [ "${#CLONE_VOL[@]}" -gt 0 ] && RUN_ARGS+=("${CLONE_VOL[@]}")
  [ -n "$CLONE_NET" ] && RUN_ARGS+=(--network "$CLONE_NET")
  [ "${#CLONE_ENV[@]}" -gt 0 ] && RUN_ARGS+=("${CLONE_ENV[@]}")
  if [ -n "$CLONE_RESTART" ] && [ "$CLONE_RESTART" != "no" ]; then
    RUN_ARGS+=(--restart="$CLONE_RESTART")
  fi
  if [ -n "$CLONE_MEM" ] && [ "$CLONE_MEM" != "0" ]; then
    RUN_ARGS+=(--memory="$CLONE_MEM")
  fi
  # E7(2026-09-09): 资源限制一并克隆(swap=limit 即不给额外 swap; cpus 防 CPU 争抢)
  if [ -n "$CLONE_SWAP" ] && [ "$CLONE_SWAP" != "0" ]; then
    RUN_ARGS+=(--memory-swap="$CLONE_SWAP")
  fi
  if [ -n "$CLONE_NANOCPUS" ] && [ "$CLONE_NANOCPUS" != "0" ]; then
    RUN_ARGS+=("--cpus=$(awk -v n="$CLONE_NANOCPUS" 'BEGIN{print n/1000000000}')")
  fi
  RUN_ARGS+=("$IMAGE")
}

wait_http_ok() {  # $1=url $2=超时秒
  [ -n "${PANWATCH_DEPLOY_STUB:-}" ] && return 0
  local _deadline=$(( $(date +%s) + $2 ))
  while [ "$(date +%s)" -lt "$_deadline" ]; do
    curl -fsS --max-time 5 "$1" >/dev/null 2>&1 && return 0
    sleep 3
  done
  return 1
}

dkq() {  # docker 静默执行; stub 模式回显参数(供 scripts/tests/test_deploy_script.sh 断言参数完整性), 退出码原样透传
  if [ -n "${PANWATCH_DEPLOY_STUB:-}" ]; then "$@"; return; fi
  "$@" >/dev/null 2>&1
}

rebuild_container() {
  echo "▶ 重建容器..."
  if $DOCKER ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER}$"; then
    harvest_existing_config "$CONTAINER"
    echo "  ✳️ 克隆现有容器配置: env=${#CLONE_ENV[@]} 挂载=${#CLONE_VOL[@]} 端口=${#CLONE_PORT[@]} 网络=${CLONE_NET:-默认}"
  else
    default_config
    echo "  ✳️ 无现有容器, 使用默认配置(全新安装)"
  fi

  # ① 先以临时名+备用端口 8001 create+start: 参数有问题在这一步失败, 旧容器毫发无损
  compose_run_args "${CONTAINER}_new" 8001
  if ! dkq $DOCKER create "${RUN_ARGS[@]}"; then
    echo "  ❌ 新容器 create 失败(旧容器保持运行), 参数如下:" >&2
    $DOCKER create "${RUN_ARGS[@]}" >&2 || true
    exit 1
  fi
  dkq $DOCKER start "${CONTAINER}_new"

  # ② 备用端口上验健康; 不过就回滚删新容器, 旧容器全程在跑
  if ! wait_http_ok "http://127.0.0.1:8001/api/health" 60; then
    echo "  ❌ 新容器(端口 8001)健康检查未通过, 回滚(旧容器保持运行)" >&2
    $DOCKER logs --tail 30 "${CONTAINER}_new" >&2 2>/dev/null || true
    $DOCKER rm -f "${CONTAINER}_new" >/dev/null 2>&1 || true
    exit 1
  fi
  echo "  ✅ 新容器(端口 8001)健康"

  # ②b 防御校验必须在换名之前(失败可无损回滚): WEB_HOST 丢了外部访问全 502
  if [ -z "${PANWATCH_DEPLOY_STUB:-}" ]; then
    if ! $DOCKER inspect "${CONTAINER}_new" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -q '^WEB_HOST=0.0.0.0$'; then
      echo "  ❌ WEB_HOST=0.0.0.0 未注入(外部访问会全 502), 回滚(旧容器保持运行)" >&2
      $DOCKER rm -f "${CONTAINER}_new" >/dev/null 2>&1 || true
      exit 1
    fi
  fi

  # ③ 换正式名+正式端口: 此时才下线旧容器(停机窗口 = 新容器启动时间)
  dkq $DOCKER rm -f "$CONTAINER"
  dkq $DOCKER stop "${CONTAINER}_new"
  dkq $DOCKER rm "${CONTAINER}_new"
  compose_run_args "$CONTAINER" ""
  if ! dkq $DOCKER create "${RUN_ARGS[@]}"; then
    echo "  ❌ 正式容器 create 失败(临时容器已删, 请检查上方 docker 报错)" >&2
    $DOCKER create "${RUN_ARGS[@]}" >&2 || true
    exit 1
  fi
  dkq $DOCKER start "$CONTAINER"
  wait_http_ok "http://127.0.0.1:8000/api/health" 60 || echo "  ⚠️ 端口 8000 暂未响应(启动中), 以冒烟门禁为准"

  # ④ 防御校验(弱项仅告警, 以克隆到的配置为预期; stub 测试跳过)
  if [ -z "${PANWATCH_DEPLOY_STUB:-}" ]; then
    _mem=$($DOCKER inspect "$CONTAINER" --format '{{.HostConfig.Memory}}')
    if [ -n "$CLONE_MEM" ] && [ "$CLONE_MEM" != "0" ] && { [ -z "$_mem" ] || [ "$_mem" = "0" ]; }; then
      echo "  ⚠️ 内存限制未生效(HostConfig.Memory=$_mem)"
    fi
    _rp=$($DOCKER inspect "$CONTAINER" --format '{{.HostConfig.RestartPolicy.Name}}')
    if [ -n "$CLONE_RESTART" ] && [ "$CLONE_RESTART" != "no" ] && [ "$_rp" != "$CLONE_RESTART" ]; then
      echo "  ⚠️ 重启策略未生效(RestartPolicy=$_rp, 预期 $CLONE_RESTART)"
    fi
  fi
  echo "  ✅ 容器已重建"
}

# 主流程
case "${1:-}" in
  --list)
    echo "部署清单:"
    for f in "${FILES[@]}"; do
      [ -f "$REPO_DIR/$f" ] && echo "  ✅ $f" || echo "  ⚠️ $f"
    done
    ;;
  --full)
    rebuild_container
    deploy_files
    $DOCKER restart "$CONTAINER" >/dev/null
    echo "✅ 完整重建完成"
    ;;
  *)
    # 检查容器是否存在
    if $DOCKER ps -a --format '{{.Names}}' | grep -q "^$CONTAINER$"; then
      deploy_files
      $DOCKER restart "$CONTAINER" >/dev/null
      echo "✅ 代码已部署并重启"
    else
      echo "⚠️ 容器不存在,执行完整重建"
      rebuild_container
      deploy_files
      $DOCKER restart "$CONTAINER" >/dev/null
      echo "✅ 容器创建+代码部署完成"
    fi
    ;;
esac

sleep 10
echo ""
echo "▶ 健康检查:"
$DOCKER ps --format "{{.Names}} {{.Status}}" | grep "$CONTAINER" || echo "⚠️ 容器未运行"
curl -s -o /dev/null -w "  http://localhost:8000 → %{http_code}\n" http://localhost:8000/ || echo "  ⚠️ 服务未响应"
echo ""
echo "▶ 预测引擎检查:"
if curl -s -o /dev/null -w "%{http_code}" http://localhost:8010/health 2>/dev/null | grep -q 200; then
  echo "  ✅ 预测引擎运行中 (:8010)"
else
  echo "  ⚠️ 预测引擎未运行,启动: sudo systemctl start panwatch-forecast"
  sudo systemctl start panwatch-forecast 2>/dev/null || true
fi
echo "▶ 同步 LLM 模型配置(设置页默认模型→引擎):"
bash ~/.hermes/scripts/sync_forecast_llm.sh 2>/dev/null | tail -1
echo ""
echo "▶ 主机预测引擎代码部署(从 git 拉最新):"
if [ -f "$REPO_DIR/forecast_server.py" ]; then
  cp "$REPO_DIR/forecast_server.py" /home/ubuntu/forecast_server.py
  mkdir -p /home/ubuntu/forecast_lib
  cp "$REPO_DIR"/forecast_lib/*.py /home/ubuntu/forecast_lib/ 2>/dev/null
  echo "  ✅ 引擎代码已同步,重启服务:"
  sudo systemctl restart panwatch-forecast 2>/dev/null || echo "  ⚠️ systemd 服务不存在,用 nohup 手动启动"
  echo ""
fi
# 0.1 (2026-09-08): 发版后冒烟门禁 —— smoke_test.py 全端点过一遍, FAIL 即部署失败。
# PANWATCH_SKIP_SMOKE=1 供 stub 测试跳过(无真实容器)。
if [ "${PANWATCH_SKIP_SMOKE:-0}" != "1" ]; then
  echo "▶ 冒烟门禁(post_deploy_smoke):"
  SMOKE_SCRIPT="${SMOKE_SCRIPT:-$SCRIPT_DIR/../scripts/smoke_test.py}" \
    bash "$SCRIPT_DIR/../scripts/post_deploy_smoke.sh" \
    || { echo "❌ 冒烟门禁未通过, 本次部署判定为失败(容器仍在运行, 便于排查)"; exit 1; }
fi
echo "=============================================="
echo " ✅ 完成。请使用上方输出的账号密码登录，并立即修改密码。"
echo "    wudao token: $([ -n "$TOKEN" ] && echo '已配置(环境变量)' || echo '未配置,用 --full 时加 WUDAO_MCP_TOKEN=<token>')"
echo "=============================================="
