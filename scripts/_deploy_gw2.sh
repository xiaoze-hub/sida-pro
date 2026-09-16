#!/bin/bash
set -euo pipefail
CT=panwatch
cp /mnt/c/Users/tianxiang/sida-work/src/web/api/skills_gateway.py /tmp/skills_gateway.py
docker cp /tmp/skills_gateway.py "$CT:/app/src/web/api/skills_gateway.py"
docker exec -u root "$CT" chown app:app /app/src/web/api/skills_gateway.py
docker exec -u app "$CT" python -m compileall -q /app/src/web/api/skills_gateway.py
docker restart "$CT"
sleep 20
for i in $(seq 1 24); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' "$CT" 2>/dev/null || echo unknown)
  echo "[$i] $ST"
  [ "$ST" = "healthy" ] && break
  sleep 5
done
curl -sS -m 15 -o /dev/null -w "health=%{http_code}\n" http://127.0.0.1:8000/api/health
echo DEPLOY_OK
