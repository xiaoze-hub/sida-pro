#!/bin/bash
# SIDA 发版后冒烟门禁 (2026-08-21; 2026-09-08 0.1 参数化+失败面收紧)
# 用法: bash post_deploy_smoke.sh [web密码]
#       可用环境变量覆盖: PW_CONTAINER(容器名, 默认 panwatch)
#                         SMOKE_LOG(日志路径, 默认 /home/ubuntu/backups/smoke.log)
#                         SMOKE_SCRIPT(smoke_test.py 路径, 默认 /home/ubuntu/scripts/smoke_test.py)
# 流程: 等 $PW_CONTAINER healthy(最长120s) → 跑 smoke_test.py → 结果追加 smoke.log
# 退出码: smoke_test.py 的退出码原样透传; 工具缺失也按 FAIL 处理(门禁不得静默假绿)
set -uo pipefail
PW="${1:-}"
PW_CONTAINER="${PW_CONTAINER:-panwatch}"
LOG="${SMOKE_LOG:-/home/ubuntu/backups/smoke.log}"
SMOKE_SCRIPT="${SMOKE_SCRIPT:-/home/ubuntu/scripts/smoke_test.py}"
BASE=http://127.0.0.1:8000
DK="${SUDO:-sudo} docker"

mkdir -p "$(dirname "$LOG")" 2>/dev/null || true

echo "[$(date '+%F %T')] ===== post-deploy smoke start =====" >> "$LOG"

# 0) 冒烟工具必须存在(缺失就响亮失败, 不许静默假绿)
if [ ! -f "$SMOKE_SCRIPT" ]; then
  echo "[$(date '+%F %T')] FAIL smoke_test.py 不存在: $SMOKE_SCRIPT (SMOKE_SCRIPT 可指定路径)" | tee -a "$LOG"
  exit 1
fi
if ! python3 -c "import requests" 2>/dev/null; then
  echo "[$(date '+%F %T')] FAIL 主机 python3 缺 requests, 装不上冒烟跑不了: pip3 install requests" | tee -a "$LOG"
  exit 1
fi

# 1) 等容器 healthy
ST=unknown
for i in $(seq 1 24); do
  ST=$($DK inspect --format '{{.State.Health.Status}}' "$PW_CONTAINER" 2>/dev/null || echo unknown)
  if [ "$ST" = "healthy" ]; then
    echo "[$(date '+%F %T')] container healthy after $((i*5))s" >> "$LOG"
    break
  fi
  sleep 5
done
if [ "$ST" != "healthy" ]; then
  echo "[$(date '+%F %T')] FAIL container not healthy after 120s" | tee -a "$LOG"
  exit 1
fi

# 2) 拿 token(容器内签发, 不依赖明文密码)
TOKEN=$($DK exec "$PW_CONTAINER" python -c "
import sys; sys.path.insert(0, '/app')
from src.web.database import SessionLocal
from src.web.api.auth import create_token
from src.web.models import User
db = SessionLocal()
admin = db.query(User).filter(User.username=='admin').first()
tok, _ = create_token(admin)
open('/tmp/.smoke_tok','w').write(tok)
" >/dev/null 2>&1 && $DK exec "$PW_CONTAINER" cat /tmp/.smoke_tok)
$DK exec "$PW_CONTAINER" rm -f /tmp/.smoke_tok >/dev/null 2>&1

# 3) 冒烟(退出码为准, 输出留档)
RC=0
if [ -n "$TOKEN" ]; then
  OUT=$(python3 "$SMOKE_SCRIPT" --base "$BASE" --token "$TOKEN" 2>&1) || RC=$?
else
  OUT=$(python3 "$SMOKE_SCRIPT" --base "$BASE" --no-auth 2>&1) || RC=$?
fi
echo "$OUT" >> "$LOG"
echo "$OUT" | tail -2
echo "[$(date '+%F %T')] ===== done (exit=$RC) =====" >> "$LOG"
exit "$RC"
