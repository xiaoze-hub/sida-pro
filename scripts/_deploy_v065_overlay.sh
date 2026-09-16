#!/bin/bash
# v0.6.5 覆盖层部署 runbook (照 v0.5.x 既定路径)
# 本机 WSL = 生产; 容器 panwatch
set -euo pipefail
REPO=/root/sida-pro
VER=v0.6.5
CT=panwatch
BAK=/root/app_backup_pre_v065_$(date +%Y%m%d_%H%M%S).tar.gz
OVERLAY=/root/overlay_v065.tar

echo "=== 1) 备份容器代码面(排除 data/static-data/downloads/node_modules/__pycache__) ==="
docker exec -u root "$CT" sh -c "cd /app && tar czf /tmp/backup.tgz --exclude=data --exclude=static-data --exclude=downloads --exclude=node_modules --exclude=__pycache__ ."
docker cp "$CT:/tmp/backup.tgz" "$BAK"
docker exec -u root "$CT" rm -f /tmp/backup.tgz
ls -lh "$BAK"

echo "=== 2) 确认无 nohup 回填在跑 ==="
docker exec "$CT" sh -c "ps aux | grep -E 'nohup|backfill' | grep -v grep || echo 'no backfill processes'"

echo "=== 3) 打覆盖层: git archive $VER + frontend/dist → static/ ==="
cd "$REPO"
rm -rf /tmp/overlay_v065 && mkdir -p /tmp/overlay_v065
# git archive 是**普通 tar**(不是 gzip)
git archive --format=tar -o /tmp/overlay_code.tar "$VER"
mkdir -p /tmp/overlay_v065/_code
tar xf /tmp/overlay_code.tar -C /tmp/overlay_v065/_code
# 前端产物进 static/
if [ ! -d "$REPO/frontend/dist" ] || [ -z "$(ls -A "$REPO/frontend/dist" 2>/dev/null)" ]; then
  echo "ERROR: frontend/dist 为空, 先 build"; exit 1
fi
mkdir -p /tmp/overlay_v065/_code/static
cp -a "$REPO/frontend/dist/." /tmp/overlay_v065/_code/static/
cd /tmp/overlay_v065/_code
tar cf "$OVERLAY" .
cd /
ls -lh "$OVERLAY"
echo "overlay items: $(tar tf "$OVERLAY" | wc -l)"

echo "=== 4) docker cp 进容器 /tmp ==="
docker cp "$OVERLAY" "$CT:/tmp/overlay_v065.tar"

echo "=== 5) root 解包 --overwrite (必须 tar xf, 不是 xzf) ==="
docker exec -u root "$CT" sh -c "cd /app && tar xf /tmp/overlay_v065.tar --overwrite"
echo "TAR_EXIT=$?"

echo "=== 6) chown + compileall (缺一会 worker Child process died) ==="
docker exec -u root "$CT" chown -R app:app /app
docker exec -u app "$CT" python -m compileall -q /app/src /app/server.py
echo "COMPILE_OK"

echo "=== 7) 核对 VERSION 再重启 ==="
docker exec "$CT" cat /app/VERSION
docker restart "$CT"

echo "=== 8) 等 healthy ==="
for i in $(seq 1 36); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' "$CT" 2>/dev/null || echo unknown)
  echo "  [$i] $ST"
  [ "$ST" = "healthy" ] && break
  sleep 5
done

echo "=== 9) /api/version + health ==="
sleep 2
curl -sS http://127.0.0.1:8000/api/version || true
echo
curl -sS http://127.0.0.1:8000/api/health | head -c 400 || true
echo

echo "=== 10) 清理容器内 /tmp 覆盖层 ==="
docker exec -u root "$CT" rm -f /tmp/overlay_v065.tar
echo "DEPLOY_SCRIPT_DONE"
