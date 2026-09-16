#!/bin/bash
set -euo pipefail
CT=panwatch
IMG=panwatch:v0.6.9
docker inspect "$CT" --format '{{range .Config.Env}}{{println .}}{{end}}' > /tmp/env_69.txt
grep -v '^WEB_WORKERS=' /tmp/env_69.txt > /tmp/env_69b.txt
echo "WEB_WORKERS=4" >> /tmp/env_69b.txt
docker stop "$CT"
docker rm "$CT"
ENVARGS=()
while IFS= read -r line; do
  [ -z "$line" ] && continue
  ENVARGS+=("-e" "$line")
done < /tmp/env_69b.txt
docker run -d --name "$CT" --restart=always -p 8000:8000 --network panwatch-net \
  -v panwatch-data:/app/data -v panwatch-tck:/app/data/tck \
  --memory 1500m --memory-swap 1500m --cpus 1.5 \
  "${ENVARGS[@]}" "$IMG"
echo "waiting healthy (90s start-period expected)..."
for i in $(seq 1 48); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' "$CT" 2>/dev/null || echo unknown)
  echo "[$i] $ST"
  [ "$ST" = "healthy" ] && break
  sleep 5
done
docker exec "$CT" cat /app/VERSION
docker inspect "$CT" --format 'Image={{.Config.Image}}'
docker exec "$CT" sh -c 'echo WEB_WORKERS=$WEB_WORKERS'
curl -sS -m 15 -o /dev/null -w "health=%{http_code}\n" http://127.0.0.1:8000/api/health
curl -sS http://127.0.0.1:8000/api/version; echo
echo RECREATE_OK
