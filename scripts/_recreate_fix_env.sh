#!/bin/bash
set -euo pipefail
CT=panwatch
IMG=panwatch:v0.6.6
echo "=== stop broken container ==="
docker stop $CT 2>/dev/null || true
docker rm $CT 2>/dev/null || true

ENVARGS=()
while IFS= read -r line; do
  [ -z "$line" ] && continue
  ENVARGS+=("-e" "$line")
done < /tmp/panwatch_env.txt
echo "env_count=${#ENVARGS[@]}"

docker run -d --name $CT \
  --restart=always \
  -p 8000:8000 \
  --network panwatch-net \
  -v panwatch-data:/app/data \
  -v panwatch-tck:/app/data/tck \
  --memory 1500m --memory-swap 1500m --cpus 1.5 \
  "${ENVARGS[@]}" \
  $IMG

echo "=== env present? ==="
docker inspect $CT --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E 'SIDA_DB|REDIS|AUTH_USER|WEB_HOST' | sed 's/PASSWORD=.*/PASSWORD=***/'

for i in $(seq 1 36); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' $CT 2>/dev/null || echo unknown)
  echo "[$i] $ST"
  [ "$ST" = "healthy" ] && break
  sleep 5
done

echo "=== verify ==="
docker exec $CT cat /app/VERSION
docker inspect $CT --format 'Image={{.Config.Image}}'
docker exec $CT python -c "
import sys; sys.path.insert(0,'/app')
from src.web.database import SessionLocal
from src.web.models import Stock
db=SessionLocal()
print('stocks', db.query(Stock).count())
"
curl -sS http://127.0.0.1:8000/api/version; echo
curl -sS -o /dev/null -w "health=%{http_code}\n" http://127.0.0.1:8000/api/health
docker exec $CT python -c "import thsdk; print('thsdk ok')"
TOKEN=$(cat /mnt/c/Users/tianxiang/sida-work/data/admin_token.txt | tr -d '\r\n')
curl -sS -o /tmp/tm.json -H "Authorization: Bearer $TOKEN" -w "board=%{http_code}\n" \
  "http://127.0.0.1:8000/api/theme-mood/board?window=20&top=5"
echo FIXED_DONE
