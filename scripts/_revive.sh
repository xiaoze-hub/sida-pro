#!/bin/bash
echo "=== logs ==="
docker logs panwatch --tail 40 2>&1
echo "=== status ==="
docker inspect panwatch --format 'Status={{.State.Status}} Health={{.State.Health.Status}} Restarts={{.RestartCount}}'
docker inspect panwatch --format '{{json .State.Health}}' | head -c 800
echo
echo "=== compileall+restart ==="
docker exec -u root panwatch chown -R app:app /app/src /app/server.py /app/static 2>/dev/null || true
docker exec -u app panwatch python -m compileall -q /app/src /app/server.py 2>&1 | tail -3
docker restart panwatch
sleep 20
for i in $(seq 1 20); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' panwatch 2>/dev/null || echo unknown)
  echo "[$i] $ST"
  [ "$ST" = "healthy" ] && break
  sleep 5
done
curl -sS -m 15 -o /dev/null -w "health=%{http_code}\n" http://127.0.0.1:8000/api/health
