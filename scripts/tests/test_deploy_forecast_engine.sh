#!/bin/bash
# 预测引擎部署脚本 stub 测试(裸进程形态契约)。
#
# 钉死的关键事实(改脚本时若破坏其一, 这里立刻红):
#   ① 代码同步到 INSTALL_DIR(forecast_server.py + forecast_lib/);
#   ② env 含 FORECAST_HOST=0.0.0.0 —— 容器侧 panwatch 经 docker 网关访问所必需,
#      听 127.0.0.1 会让容器连接被拒(= 引擎"起来了但主服务够不着");
#   ③ env 含 FORECAST_DB_PATH;
#   ④ unit 指向 venv python + EnvironmentFile + Restart=always + MemoryMax;
#   ⑤ systemctl daemon-reload / enable --now / restart 都被调用;
#   ⑥ 成功打印 FORECAST_DEPLOY_OK。
# 不触网、不建 venv、不真装(FORECAST_DEPLOY_STUB=1 + SYSTEMCTL 桩)。
# 用法: bash scripts/tests/test_deploy_forecast_engine.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY="$SCRIPT_DIR/../../deploy/deploy_forecast_engine.sh"
SRC="$(cd "$SCRIPT_DIR/../.." && pwd)"
PASS=0; FAIL=0
ok()  { echo "PASS $1"; PASS=$((PASS+1)); }
bad() { echo "FAIL $1"; FAIL=$((FAIL+1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
INSTALL="$TMP/install"
UNITS="$TMP/units"

OUT=$(FORECAST_DEPLOY_STUB=1 FORECAST_SKIP_DEPS=1 \
      FORECAST_SOURCE_DIR="$SRC" FORECAST_INSTALL_DIR="$INSTALL" \
      FORECAST_SYSTEMD_DIR="$UNITS" SYSTEMCTL="echo systemctl" \
      bash "$DEPLOY" 2>&1; echo "RC=$?")

echo "$OUT" | grep -q "RC=0" && ok "脚本退出码 0" || bad "脚本未以 0 退出"
echo "$OUT" | grep -q "FORECAST_DEPLOY_OK" && ok "成功标记 FORECAST_DEPLOY_OK" || bad "缺成功标记 FORECAST_DEPLOY_OK"

[ -f "$INSTALL/forecast_server.py" ] && ok "forecast_server.py 已同步" || bad "forecast_server.py 未同步"
[ -f "$INSTALL/forecast_lib/ai_referee.py" ] && ok "forecast_lib/ai_referee.py 已同步" || bad "forecast_lib/ 未同步"
[ -f "$INSTALL/forecast_lib/forecast_models.py" ] && ok "forecast_lib/forecast_models.py 已同步" || bad "forecast_lib/ 不完整"

ENVF="$INSTALL/forecast.env"
[ -f "$ENVF" ] && ok "env 文件已生成" || bad "env 文件未生成"
grep -q "^FORECAST_HOST=0.0.0.0$" "$ENVF" && ok "env FORECAST_HOST=0.0.0.0(容器可达, 非回环)" || bad "env 缺 FORECAST_HOST=0.0.0.0"
grep -q "^FORECAST_DB_PATH=.*panwatch_forecast.db" "$ENVF" && ok "env 含 FORECAST_DB_PATH 历史库" || bad "env 缺 FORECAST_DB_PATH"
grep -q "^SIDA_MAIN_API_URL=http://127.0.0.1:8000" "$ENVF" && ok "env 主服务地址指向宿主机 8000" || bad "env 主服务地址不对"

UNIT="$UNITS/panwatch-forecast.service"
[ -f "$UNIT" ] && ok "unit 已生成" || bad "unit 未生成"
grep -q "ExecStart=$INSTALL/venv/bin/python3 $INSTALL/forecast_server.py" "$UNIT" && ok "unit ExecStart 指向 venv python + forecast_server.py" || bad "unit ExecStart 不对"
grep -q "EnvironmentFile=$ENVF" "$UNIT" && ok "unit 读 forecast.env" || bad "unit 未接 EnvironmentFile"
grep -q "^Restart=always" "$UNIT" && ok "unit Restart=always(常驻)" || bad "unit 缺 Restart=always"
grep -q "^MemoryMax=4G" "$UNIT" && ok "unit MemoryMax=4G(纯推理内存上限)" || bad "unit 缺 MemoryMax"
grep -q "^WantedBy=multi-user.target" "$UNIT" && ok "unit 开机自启(WantedBy)" || bad "unit 缺 WantedBy"

echo "$OUT" | grep -q "systemctl daemon-reload" && ok "调用 systemctl daemon-reload" || bad "未调用 daemon-reload"
echo "$OUT" | grep -q "systemctl enable --now panwatch-forecast" && ok "调用 systemctl enable --now" || bad "未 enable --now"
echo "$OUT" | grep -q "systemctl restart panwatch-forecast" && ok "调用 systemctl restart" || bad "未 restart"

# 容器化形态被否决: 脚本不得再拉 *-forecast 镜像(docker pull)
if grep -qE "docker (pull|run).*(forecast|-forecast)" "$DEPLOY"; then
  bad "脚本仍含 forecast 容器化命令(应为裸进程形态)"
else
  ok "脚本为裸进程形态(无 forecast 容器化命令)"
fi

# 源码缺失时显式失败(不静默)
OUT2=$(FORECAST_DEPLOY_STUB=1 FORECAST_SKIP_DEPS=1 \
       FORECAST_SOURCE_DIR="$TMP/empty" FORECAST_INSTALL_DIR="$TMP/i2" \
       FORECAST_SYSTEMD_DIR="$TMP/u2" SYSTEMCTL="echo systemctl" \
       bash "$DEPLOY" 2>&1; echo "RC=$?")
echo "$OUT2" | grep -q "RC=1" && ok "源码缺失 → 退出码 1(显式失败)" || bad "源码缺失未显式失败"

echo ""
echo "=== deploy_forecast_engine stub test: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
