#!/bin/bash
set -euo pipefail
CT=panwatch
# backend hotfix
cp /mnt/c/Users/tianxiang/sida-work/src/core/auth_tokens.py /tmp/auth_tokens.py
docker cp /tmp/auth_tokens.py $CT:/app/src/core/auth_tokens.py
docker exec -u root $CT chown app:app /app/src/core/auth_tokens.py
docker exec -u root $CT rm -f /app/src/core/marketdata_authoritative_sources.py
docker exec -u app $CT python -m compileall -q /app/src/core/auth_tokens.py
# frontend static from Windows dist if present
if [ -f /mnt/c/Users/tianxiang/sida-work/frontend/dist/index.html ]; then
  docker cp /mnt/c/Users/tianxiang/sida-work/frontend/dist/. $CT:/app/static/
  docker exec -u root $CT chown -R app:app /app/static
fi
docker restart $CT
for i in $(seq 1 24); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' $CT 2>/dev/null || echo unknown)
  echo "[$i] $ST"
  [ "$ST" = "healthy" ] && break
  sleep 5
done
docker exec $CT test ! -f /app/src/core/marketdata_authoritative_sources.py && echo "dead file removed"
curl -sS http://127.0.0.1:8000/api/version; echo
curl -sS -o /dev/null -w "health=%{http_code}\n" http://127.0.0.1:8000/api/health
echo DEPLOY_OK
