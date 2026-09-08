#!/bin/bash
# 0.1 (2026-09-08) 部署脚本 stub 测试 — 防止"续行块内注释吞参数"事故复发。
# DOCKER=echo 桩跑 --full, 断言 create/run 参数完整包含关键项;
# 再用 create 即失败的桩验证: 旧容器在重建失败时不会被先行删除。
# 用法: bash scripts/tests/test_deploy_script.sh   (CI: build-push-acr.yml gates)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY="$SCRIPT_DIR/../../deploy/deploy_panwatch.sh"
PASS=0; FAIL=0

ok()  { echo "PASS $1"; PASS=$((PASS+1)); }
bad() { echo "FAIL $1"; FAIL=$((FAIL+1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/repo/.git"

# 场景1: DOCKER=echo 干跑 --full → 关键参数必须完整出现在 create 参数里
OUT=$(PANWATCH_REPO="$TMP/repo" PANWATCH_DEPLOY_STUB=1 PANWATCH_SKIP_SMOKE=1 \
      DOCKER=echo AUTH_PASSWORD=stubpass \
      bash "$DEPLOY" --full 2>&1)

echo "$OUT" | grep -q -- "-e WEB_HOST=0.0.0.0" && ok "create 参数含 WEB_HOST=0.0.0.0" || bad "create 参数缺 WEB_HOST=0.0.0.0"
echo "$OUT" | grep -q -- "--memory=1073741824" && ok "create 参数含 memory 限制" || bad "create 参数缺 memory 限制"
echo "$OUT" | grep -q -- "--restart=unless-stopped" && ok "create 参数含重启策略" || bad "create 参数缺重启策略"
echo "$OUT" | grep -q -- "-p 8000:8000" && ok "正式端口 8000:8000 在参数里" || bad "缺正式端口 8000:8000"
echo "$OUT" | grep -q -- "-p 8001:8000" && ok "临时容器走备用端口 8001(先验证后切换)" || bad "缺备用端口 8001(先启新后删旧的顺序未实现)"
echo "$OUT" | grep -q -- "xzxwz:latest" && ok "镜像名在参数末尾" || bad "镜像名缺失"
echo "$OUT" | grep -q -- "-e AUTH_PASSWORD=stubpass" && ok "AUTH_PASSWORD 注入" || bad "AUTH_PASSWORD 未注入"
echo "$OUT" | grep -q -- "-v panwatch-data:/app/data" && ok "数据卷为生产同款连字符命名 panwatch-data" || bad "数据卷名不是 panwatch-data(生产实测卷名)"
echo "$OUT" | grep -q -- "-e: command not found" && bad "出现续行断裂(-e: command not found)" || ok "无续行断裂"

# 场景1 补充: 旧容器删除(rm -f panwatch)只允许发生在临时容器健康验证之后
LINE_NEW_OK=$(echo "$OUT" | grep -n "新容器(端口 8001)健康" | head -1 | cut -d: -f1)
LINE_RM_OLD=$(echo "$OUT" | grep -n "rm -f panwatch$" | head -1 | cut -d: -f1)
if [ -n "${LINE_NEW_OK:-}" ] && [ -n "${LINE_RM_OLD:-}" ] && [ "$LINE_NEW_OK" -lt "$LINE_RM_OLD" ]; then
  ok "删旧容器发生在健康验证之后"
else
  bad "删旧容器顺序不对(必须在健康验证之后)"
fi

# 场景2: create 即失败 → 脚本退出非 0, 且全程未执行任何 rm -f(旧容器保命)
cat > "$TMP/dockerfail" <<'EOF'
#!/bin/bash
if [ "${1:-}" = "create" ]; then echo "  simulated: invalid image" >&2; exit 1; fi
echo "$@"
EOF
chmod +x "$TMP/dockerfail"
OUT2=$(PANWATCH_REPO="$TMP/repo" PANWATCH_DEPLOY_STUB=1 PANWATCH_SKIP_SMOKE=1 \
       DOCKER="$TMP/dockerfail" AUTH_PASSWORD=stubpass \
       bash "$DEPLOY" --full 2>&1; echo "RC=$?")
RC2=$(echo "$OUT2" | grep -o "RC=[0-9]*" | tail -1 | cut -d= -f2)
[ "$RC2" != "0" ] && ok "create 失败 → 脚本退出非 0" || bad "create 失败但脚本继续走(退出码 0)"
echo "$OUT2" | grep -q "rm -f" && bad "create 失败场景出现了 rm -f(旧容器有被删风险)" || ok "create 失败时全程无 rm -f(旧容器无损)"

# 场景3: 冒烟门禁确实被接线(源码含调用), stub 跳过开关有效
grep -q "post_deploy_smoke.sh" "$DEPLOY" && ok "部署脚本已接线 post_deploy_smoke.sh" || bad "冒烟门禁未接线"

echo ""
echo "=== deploy stub test: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
