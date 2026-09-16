#!/bin/bash
set -euo pipefail
CT=panwatch
for f in src/web/api/skills_gateway.py server.py; do
  base=$(basename "$f")
  cp "/mnt/c/Users/tianxiang/sida-work/$f" "/tmp/$base"
  docker cp "/tmp/$base" "$CT:/app/$f"
  docker exec -u root "$CT" chown app:app "/app/$f"
done
docker exec -u app "$CT" python -m compileall -q /app/src/web/api/skills_gateway.py /app/server.py
# 多 worker
docker inspect "$CT" --format '{{range .Config.Env}}{{println .}}{{end}}' > /tmp/env_gw.txt
grep -v '^WEB_WORKERS=' /tmp/env_gw.txt > /tmp/env_gw2.txt
echo "WEB_WORKERS=4" >> /tmp/env_gw2.txt
docker stop "$CT"
docker rm "$CT"
ENVARGS=()
while IFS= read -r line; do
  [ -z "$line" ] && continue
  ENVARGS+=("-e" "$line")
done < /tmp/env_gw2.txt
docker run -d --name "$CT" --restart=always -p 8000:8000 --network panwatch-net \
  -v panwatch-data:/app/data -v panwatch-tck:/app/data/tck \
  --memory 1500m --memory-swap 1500m --cpus 1.5 \
  "${ENVARGS[@]}" panwatch:v0.6.8
echo "waiting healthy..."
for i in $(seq 1 40); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' "$CT" 2>/dev/null || echo unknown)
  echo "[$i] $ST"
  [ "$ST" = "healthy" ] && break
  sleep 5
done
curl -sS -m 15 -o /dev/null -w "health=%{http_code}\n" http://127.0.0.1:8000/api/health
curl -sS http://127.0.0.1:8000/api/version; echo
echo DEPLOY_OK
