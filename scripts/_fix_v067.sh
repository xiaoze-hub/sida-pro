#!/bin/bash
docker exec -u root sida chown -R app:app /app/src /app/server.py /app/static /app/packages 2>/dev/null || true
docker exec -u app sida python -m compileall -q /app/src /app/server.py
docker restart sida
sleep 25
docker logs sida --tail 25 2>&1
docker inspect sida --format '{{.State.Health.Status}}'
for i in $(seq 1 20); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' sida 2>/dev/null || echo unknown)
  echo "[$i] $ST"
  [ "$ST" = "healthy" ] && break
  sleep 5
done
curl -sS http://127.0.0.1:8000/api/version; echo
curl -sS -o /dev/null -w "health=%{http_code}\n" http://127.0.0.1:8000/api/health
echo FIX_DONE
