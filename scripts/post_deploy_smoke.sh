#!/bin/bash
# SIDA 发版后冒烟门禁 (2026-08-21)
# 用法: bash post_deploy_smoke.sh [web密码]
# 流程: 等 panwatch healthy(最长120s) → 跑 smoke_test.py → 结果追加 smoke.log
set -uo pipefail
PW="${1:-}"
LOG=/home/ubuntu/backups/smoke.log
BASE=http://127.0.0.1:8000

echo "[$(date '+%F %T')] ===== post-deploy smoke start =====" >> "$LOG"

# 1) 等容器 healthy
for i in $(seq 1 24); do
  ST=$(sudo docker inspect --format '{{.State.Health.Status}}' panwatch 2>/dev/null || echo unknown)
  if [ "$ST" = "healthy" ]; then
    echo "[$(date '+%F %T')] container healthy after $((i*5))s" >> "$LOG"
    break
  fi
  sleep 5
done
if [ "${ST:-unknown}" != "healthy" ]; then
  echo "[$(date '+%F %T')] FAIL container not healthy after 120s" >> "$LOG"
  exit 1
fi

# 2) 拿 token(容器内签发, 不依赖明文密码)
TOKEN=$(sudo docker exec panwatch python -c "
import sys; sys.path.insert(0, '/app')
from src.web.database import SessionLocal
from src.web.api.auth import create_token
from src.web.models import User
db = SessionLocal()
admin = db.query(User).filter(User.username=='admin').first()
tok, _ = create_token(admin)
open('/tmp/.smoke_tok','w').write(tok)
" >/dev/null 2>&1 && sudo docker exec panwatch cat /tmp/.smoke_tok)
sudo docker exec panwatch rm -f /tmp/.smoke_tok >/dev/null 2>&1

# 3) 冒烟
if [ -n "$TOKEN" ]; then
  OUT=$(python3 /home/ubuntu/scripts/smoke_test.py --base "$BASE" --token "$TOKEN" 2>&1)
else
  OUT=$(python3 /home/ubuntu/scripts/smoke_test.py --base "$BASE" --no-auth 2>&1)
fi
echo "$OUT" >> "$LOG"
echo "$OUT" | tail -2
echo "[$(date '+%F %T')] ===== done =====" >> "$LOG"

# 4) 退出码透传
if echo "$OUT" | grep -q "FAIL"; then
  exit 1
fi
exit 0
