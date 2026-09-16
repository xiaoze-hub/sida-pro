#!/bin/bash
docker exec -u root panwatch chown -R app:app /app/src /app/server.py /app/static /app/packages 2>/dev/null || true
docker exec -u app panwatch python -m compileall -q /app/src /app/server.py
docker restart panwatch
sleep 25
docker logs panwatch --tail 25 2>&1
docker inspect panwatch --format '{{.State.Health.Status}}'
for i in $(seq 1 20); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' panwatch 2>/dev/null || echo unknown)
  echo "[$i] $ST"
  [ "$ST" = "healthy" ] && break
  sleep 5
done
curl -sS http://127.0.0.1:8000/api/version; echo
curl -sS -o /dev/null -w "health=%{http_code}\n" http://127.0.0.1:8000/api/health
echo FIX_DONE
