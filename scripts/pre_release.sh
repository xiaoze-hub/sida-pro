#!/usr/bin/env bash
# 发版前本地门禁 + 设计验收巡检(P1-4, 2026-09-18)。
#
# 为什么需要它: 2026-09-18 连踩两次 —— ①只跑前端门禁就推 tag → CI 的**后端 pytest** 红
# (凭据扫描 + 真实数据断言都只在那里); ②设计稿 v3.0 的验收线如果不每次复跑, 就会退化成口号。
# 一条命令把"该跑的"和"该看的"都拉齐, 并**打印巡检数字供回填台账**。
#
# 用法:
#   SIDA_SHOT_PW=*** bash scripts/pre_release.sh          # 全跑 + 巡检生产
#   SKIP_AUDIT=1 bash scripts/pre_release.sh              # 只跑门禁(离线/赶时间)
#   BASE=http://localhost:8000 SIDA_SHOT_PW=*** bash scripts/pre_release.sh
#
# 退出码: 任一门禁失败 → 1(不要推 tag); 巡检越线 → 0(设计稿规定: 越线需书面说明, 不硬拦)
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FE="$ROOT/frontend"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY=python3
BASE="${BASE:-https://www.sida.hengsheng-elec.com}"
rc=0
say() { printf '\n=== %s ===\n' "$1"; }

say "前端: tsc -b"
( cd "$FE" && node_modules/.bin/tsc -b ) || { echo "✗ tsc 失败"; rc=1; }

say "前端: eslint"
( cd "$FE" && node_modules/.bin/eslint . ) || { echo "✗ eslint 失败"; rc=1; }

say "前端: vitest"
( cd "$FE" && node_modules/.bin/vitest run ) || { echo "✗ vitest 失败"; rc=1; }

say "前端: vite build"
( cd "$FE" && node_modules/.bin/vite build ) || { echo "✗ build 失败"; rc=1; }

say "门禁: ui-rules / 迁移 / 作用域查询 / is_pg"
( cd "$ROOT" && node scripts/check_ui_rules.mjs ) || { echo "✗ ui-rules 失败"; rc=1; }
( cd "$ROOT" && "$PY" scripts/check_migrations.py ) || { echo "✗ 迁移校验失败"; rc=1; }
( cd "$ROOT" && "$PY" scripts/check_scoped_queries.py ) || { echo "✗ 作用域查询门禁失败"; rc=1; }
( cd "$ROOT" && "$PY" scripts/check_is_pg_scope.py ) || { echo "✗ is_pg 门禁失败"; rc=1; }

say "后端: pytest(凭据扫描 + 契约 + 真实数据断言 —— 前端改动也必须跑)"
( cd "$ROOT" && "$PY" -m pytest tests/test_auth_no_default_password.py tests/test_decision_log.py \
    tests/test_caliber_compare.py tests/test_caliber_archive.py tests/test_rally_analysis.py \
    tests/test_resonance_scan.py -q -p no:cacheprovider -o addopts="" ) || { echo "✗ pytest 失败"; rc=1; }

if [ -z "${SKIP_AUDIT:-}" ]; then
  say "设计验收(终端 8 页) —— 数字回填 docs/开发计划_v3.0_20260918.md §六 台账"
  if [ -n "${SIDA_SHOT_PW:-}" ]; then
    ( cd "$ROOT" && "$PY" scripts/terminal_audit.py --base "$BASE" --json /tmp/audit_terminal.json ) \
      || echo "⚠ 有越线项: 按设计稿要求写进 CHANGELOG 说明(不硬拦)"
  else
    echo "⚠ 跳过: 需要 SIDA_SHOT_PW 环境变量(密码不入库)"
  fi
  say "设计验收(公开面 4 页)"
  if [ -n "${SIDA_SHOT_PW:-}" ]; then
    ( cd "$ROOT" && "$PY" scripts/terminal_audit.py --anon --base "$BASE" --json /tmp/audit_anon.json ) \
      || echo "⚠ 有越线项: 同上"
  else
    echo "⚠ 跳过: 需要 SIDA_SHOT_PW"
  fi
fi

say "结论"
if [ "$rc" -eq 0 ]; then
  echo "✅ 门禁全绿 —— 可以推 tag。记得: 部署落地只看生产 /api/health 的 version + docker inspect 的镜像 tag。"
else
  echo "❌ 有门禁失败 —— 不要推 tag(先修)。"
fi
exit "$rc"
