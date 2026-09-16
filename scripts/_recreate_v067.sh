#!/bin/bash
set -euo pipefail
CT=panwatch
IMG=panwatch:v0.6.7
# save env from current
docker inspect $CT --format '{{range .Config.Env}}{{println .}}{{end}}' > /tmp/panwatch_env.txt
echo "env_lines=$(wc -l < /tmp/panwatch_env.txt)"
docker stop $CT
docker rm $CT
ENVARGS=()
while IFS= read -r line; do
  [ -z "$line" ] && continue
  ENVARGS+=("-e" "$line")
done < /tmp/panwatch_env.txt
docker run -d --name $CT \
  --restart=always \
  -p 8000:8000 \
  --network panwatch-net \
  -v panwatch-data:/app/data \
  -v panwatch-tck:/app/data/tck \
  --memory 1500m --memory-swap 1500m --cpus 1.5 \
  "${ENVARGS[@]}" \
  $IMG
for i in $(seq 1 36); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' $CT 2>/dev/null || echo unknown)
  echo "[$i] $ST"
  [ "$ST" = "healthy" ] && break
  sleep 5
done
echo "=== verify ==="
docker exec $CT cat /app/VERSION
docker inspect $CT --format 'Image={{.Config.Image}}'
docker exec $CT which pg_dump || echo "pg_dump MISSING"
docker exec $CT python -c "
import sys; sys.path.insert(0,'/app')
from src.web.database import SessionLocal
from src.web.models import Stock
from src.core.trading_calendar import is_trading_day
from datetime import date
print('stocks', SessionLocal().query(Stock).count())
print('2028-03-15 trading', is_trading_day(date(2028,3,15)))
"
curl -sS http://127.0.0.1:8000/api/version; echo
curl -sS -o /dev/null -w "health=%{http_code}\n" http://127.0.0.1:8000/api/health
echo RECREATE_OK
