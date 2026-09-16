#!/bin/bash
set -euo pipefail
CT=panwatch
docker inspect $CT --format '{{range .Config.Env}}{{println .}}{{end}}' > /tmp/env.txt
grep -v '^WEB_WORKERS=' /tmp/env.txt > /tmp/env2.txt
echo "WEB_WORKERS=2" >> /tmp/env2.txt
docker stop $CT
docker rm $CT
ENVARGS=()
while IFS= read -r line; do
  [ -z "$line" ] && continue
  ENVARGS+=("-e" "$line")
done < /tmp/env2.txt
docker run -d --name $CT \
  --restart=always -p 8000:8000 --network panwatch-net \
  -v panwatch-data:/app/data -v panwatch-tck:/app/data/tck \
  --memory 1500m --memory-swap 1500m --cpus 1.5 \
  "${ENVARGS[@]}" panwatch:v0.6.7
echo "waiting..."
for i in $(seq 1 36); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' $CT 2>/dev/null || echo unknown)
  echo "[$i] $ST"
  if [ "$ST" = "healthy" ]; then
    curl -sS -m 15 -o /dev/null -w "health=%{http_code} time=%{time_total}\n" http://127.0.0.1:8000/api/health
    echo "WORKERS=2 OK"
    exit 0
  fi
  if [ "$ST" = "unhealthy" ] && [ "$i" -gt 12 ]; then
    echo "workers=2 failed, revert to 1"
    docker stop $CT
    docker rm $CT
    grep -v '^WEB_WORKERS=' /tmp/env.txt > /tmp/env3.txt
    echo "WEB_WORKERS=1" >> /tmp/env3.txt
    ENVARGS=()
    while IFS= read -r line; do
      [ -z "$line" ] && continue
      ENVARGS+=("-e" "$line")
    done < /tmp/env3.txt
    docker run -d --name $CT --restart=always -p 8000:8000 --network panwatch-net \
      -v panwatch-data:/app/data -v panwatch-tck:/app/data/tck \
      --memory 1500m --memory-swap 1500m --cpus 1.5 \
      "${ENVARGS[@]}" panwatch:v0.6.7
    for j in $(seq 1 24); do
      ST=$(docker inspect --format '{{.State.Health.Status}}' $CT 2>/dev/null || echo unknown)
      echo "  r[$j] $ST"
      [ "$ST" = "healthy" ] && break
      sleep 5
    done
    curl -sS -m 15 -o /dev/null -w "health=%{http_code}\n" http://127.0.0.1:8000/api/health
    echo "REVERTED_TO_1"
    exit 0
  fi
  sleep 5
done
echo "TIMEOUT"
exit 1
