#!/bin/bash
set -euo pipefail
CT=panwatch
cp /mnt/c/Users/tianxiang/sida-work/src/web/api/quotes.py /tmp/quotes.py
docker cp /tmp/quotes.py $CT:/app/src/web/api/quotes.py
docker exec -u root $CT chown app:app /app/src/web/api/quotes.py
docker exec -u app $CT python -m compileall -q /app/src/web/api/quotes.py
docker cp /mnt/c/Users/tianxiang/sida-work/frontend/dist/. $CT:/app/static/
docker exec -u root $CT chown -R app:app /app/static
docker restart $CT
sleep 18
for i in $(seq 1 24); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' $CT 2>/dev/null || echo unknown)
  echo "[$i] $ST"
  if [ "$ST" = "healthy" ]; then break; fi
  sleep 5
done
curl -sS -m 15 -o /dev/null -w "health=%{http_code}\n" http://127.0.0.1:8000/api/health
curl -sS http://127.0.0.1:8000/api/version; echo
# verify dark-flow-tq no longer 404
TOKEN=$(cat /mnt/c/Users/tianxiang/sida-work/data/admin_token.txt | tr -d '\r\n')
curl -sS -o /tmp/d.json -H "Authorization: Bearer $TOKEN" -w "dark=%{http_code}\n" "http://127.0.0.1:8000/api/quotes/600519/dark-flow-tq?market=CN"
head -c 120 /tmp/d.json; echo
echo DEPLOY_OK
