#!/bin/bash
set -euo pipefail
CT=panwatch
IMG=panwatch:v0.6.8
# save env
docker inspect $CT --format '{{range .Config.Env}}{{println .}}{{end}}' > /tmp/env_v068.txt
grep -v '^WEB_WORKERS=' /tmp/env_v068.txt > /tmp/env_v068b.txt
echo "WEB_WORKERS=1" >> /tmp/env_v068b.txt
docker stop $CT
docker rm $CT
ENVARGS=()
while IFS= read -r line; do
  [ -z "$line" ] && continue
  ENVARGS+=("-e" "$line")
done < /tmp/env_v068b.txt
docker run -d --name $CT \
  --restart=always \
  -p 8000:8000 \
  --network panwatch-net \
  -v panwatch-data:/app/data \
  -v panwatch-tck:/app/data/tck \
  --memory 1500m --memory-swap 1500m --cpus 1.5 \
  "${ENVARGS[@]}" \
  $IMG
echo "waiting healthy..."
for i in $(seq 1 36); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' $CT 2>/dev/null || echo unknown)
  echo "[$i] $ST"
  if [ "$ST" = "healthy" ]; then break; fi
  sleep 5
done
echo "=== verify ==="
docker exec $CT cat /app/VERSION
docker inspect $CT --format 'Image={{.Config.Image}}'
docker exec $CT python -c "
import sys; sys.path.insert(0,'/app')
from src.web.database import SessionLocal
from src.web.models import Stock, SkillApiKey
db=SessionLocal()
print('stocks', db.query(Stock).count())
print('skill_keys', db.query(SkillApiKey).count())
"
curl -sS -m 15 -o /dev/null -w "health=%{http_code}\n" http://127.0.0.1:8000/api/health
curl -sS http://127.0.0.1:8000/api/version; echo
# gateway smoke
curl -sS -m 20 -X POST http://127.0.0.1:8000/api/keys -H "Content-Type: application/json" -d '{"owner_label":"release-smoke"}' -o /tmp/k.json
python3 - <<'PY'
import json
d=json.load(open("/tmp/k.json"))
data=d.get("data") if isinstance(d,dict) and "data" in d else d
print("tier", data.get("tier"), "limit", data.get("daily_limit"))
open("/tmp/k.txt","w").write(data.get("api_key",""))
PY
KEY=$(cat /tmp/k.txt)
curl -sS -m 20 -H "X-API-Key: $KEY" -o /tmp/run.json -w "run=%{http_code}\n" -X POST -H "Content-Type: application/json" -d '{"args":{"symbol":"600519","market":"CN"}}' http://127.0.0.1:8000/api/skills/get_stock_quote/run
python3 -c "import json;d=json.load(open('/tmp/run.json'));print((d.get('data',d).get('result') or '')[:80])"
echo RELEASE_OK
