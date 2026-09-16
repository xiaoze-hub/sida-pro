#!/bin/bash
set -euo pipefail
# 回退 WEB_WORKERS=1
CT=panwatch
docker inspect "$CT" --format '{{range .Config.Env}}{{println .}}{{end}}' > /tmp/env_r.txt || true
grep -v '^WEB_WORKERS=' /tmp/env_r.txt > /tmp/env_r2.txt
echo "WEB_WORKERS=1" >> /tmp/env_r2.txt
docker stop "$CT" 2>/dev/null || true
docker rm "$CT" 2>/dev/null || true
ENVARGS=()
while IFS= read -r line; do
  [ -z "$line" ] && continue
  ENVARGS+=("-e" "$line")
done < /tmp/env_r2.txt
docker run -d --name "$CT" --restart=always -p 8000:8000 --network panwatch-net \
  -v panwatch-data:/app/data -v panwatch-tck:/app/data/tck \
  --memory 1500m --memory-swap 1500m --cpus 1.5 \
  "${ENVARGS[@]}" panwatch:v0.6.8
for i in $(seq 1 30); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' "$CT" 2>/dev/null || echo unknown)
  echo "[$i] $ST"
  [ "$ST" = "healthy" ] && break
  sleep 5
done
curl -sS -m 15 -o /dev/null -w "health=%{http_code}\n" http://127.0.0.1:8000/api/health
echo REVERT_OK
