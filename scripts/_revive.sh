#!/bin/bash
echo "=== logs ==="
docker logs sida --tail 40 2>&1
echo "=== status ==="
docker inspect sida --format 'Status={{.State.Status}} Health={{.State.Health.Status}} Restarts={{.RestartCount}}'
docker inspect sida --format '{{json .State.Health}}' | head -c 800
echo
echo "=== compileall+restart ==="
docker exec -u root sida chown -R app:app /app/src /app/server.py /app/static 2>/dev/null || true
docker exec -u app sida python -m compileall -q /app/src /app/server.py 2>&1 | tail -3
docker restart sida
sleep 20
for i in $(seq 1 20); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' sida 2>/dev/null || echo unknown)
  echo "[$i] $ST"
  [ "$ST" = "healthy" ] && break
  sleep 5
done
curl -sS -m 15 -o /dev/null -w "health=%{http_code}\n" http://127.0.0.1:8000/api/health
