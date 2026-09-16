#!/bin/bash
set -euo pipefail
CT=panwatch
docker inspect $CT --format '{{range .Config.Env}}{{println .}}{{end}}' > /tmp/panwatch_env.txt
# 移除旧 WEB_WORKERS 再加 =1
grep -v '^WEB_WORKERS=' /tmp/panwatch_env.txt > /tmp/panwatch_env2.txt
echo "WEB_WORKERS=1" >> /tmp/panwatch_env2.txt
docker stop $CT
docker rm $CT
ENVARGS=()
while IFS= read -r line; do
  [ -z "$line" ] && continue
  ENVARGS+=("-e" "$line")
done < /tmp/panwatch_env2.txt
docker run -d --name $CT \
  --restart=always \
  -p 8000:8000 \
  --network panwatch-net \
  -v panwatch-data:/app/data \
  -v panwatch-tck:/app/data/tck \
  --memory 1500m --memory-swap 1500m --cpus 1.5 \
  "${ENVARGS[@]}" \
  panwatch:v0.6.7
echo "waiting for healthy (slow start is expected)..."
for i in $(seq 1 48); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' $CT 2>/dev/null || echo unknown)
  echo "[$i] $ST"
  if [ "$ST" = "healthy" ]; then break; fi
  sleep 5
done
docker exec $CT sh -c 'echo WEB_WORKERS=$WEB_WORKERS'
curl -sS -m 20 -o /dev/null -w "health=%{http_code} time=%{time_total}\n" http://127.0.0.1:8000/api/health
curl -sS http://127.0.0.1:8000/api/version; echo
echo REVIVE_OK
