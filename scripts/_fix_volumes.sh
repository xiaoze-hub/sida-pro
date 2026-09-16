#!/bin/bash
set -euo pipefail
CT=panwatch
IMG=panwatch:v0.6.6
echo "=== volume check ==="
docker inspect $CT --format '{{json .Mounts}}' | python3 -m json.tool | head -40
docker volume ls | grep panwatch

echo "=== stop & recreate with CORRECT volume names ==="
docker stop $CT
docker rm $CT

docker inspect $CT --format '{{range .Config.Env}}{{println .}}{{end}}' > /tmp/env_bak.txt 2>/dev/null || true
# env was dumped before stop; reload from previous if empty
if [ ! -s /tmp/env_bak.txt ]; then
  # recreate env from known keys by reading from a saved file
  echo "env_bak empty, using /tmp/panwatch_env.txt from recreate script"
  cp /tmp/panwatch_env.txt /tmp/env_bak.txt
fi

ENVARGS=()
while IFS= read -r line; do
  [ -z "$line" ] && continue
  ENVARGS+=("-e" "$line")
done < /tmp/env_bak.txt

# 正确卷名: panwatch-data / panwatch-tck (连字符)
docker run -d --name $CT \
  --restart=always \
  -p 8000:8000 \
  --network panwatch-net \
  -v panwatch-data:/app/data \
  -v panwatch-tck:/app/data/tck \
  --memory 1500m --memory-swap 1500m --cpus 1.5 \
  "${ENVARGS[@]}" \
  $IMG

echo "=== mounts ==="
docker inspect $CT --format '{{json .Mounts}}' | python3 -m json.tool | head -30

for i in $(seq 1 36); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' $CT 2>/dev/null || echo unknown)
  echo "[$i] $ST"
  [ "$ST" = "healthy" ] && break
  sleep 5
done

echo "=== verify data + version ==="
docker exec $CT cat /app/VERSION
# 自选应仍有数据(不是空库)
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
echo FIX_VOLUME_DONE
