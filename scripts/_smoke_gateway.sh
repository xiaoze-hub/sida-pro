#!/bin/bash
set -uo pipefail
CT=panwatch
for f in src/web/api/skills_gateway.py src/db/models.py src/web/migrations.py src/web/app.py; do
  base=$(basename "$f")
  cp "/mnt/c/Users/tianxiang/sida-work/$f" "/tmp/$base"
  docker cp "/tmp/$base" "$CT:/app/$f"
  docker exec -u root "$CT" chown app:app "/app/$f"
done
docker exec -u app "$CT" python -m compileall -q /app/src/web/api/skills_gateway.py /app/src/db/models.py /app/src/web/migrations.py /app/src/web/app.py
docker restart "$CT"
sleep 22
for i in $(seq 1 24); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' "$CT" 2>/dev/null || echo unknown)
  echo "[$i] $ST"
  [ "$ST" = "healthy" ] && break
  sleep 5
done
curl -sS -m 15 -o /dev/null -w "health=%{http_code}\n" http://127.0.0.1:8000/api/health

# smoke register
curl -sS -m 20 -X POST http://127.0.0.1:8000/api/keys \
  -H "Content-Type: application/json" \
  -d '{"owner_label":"smoke"}' -o /tmp/key.json
python3 - <<'PY'
import json
d=json.load(open("/tmp/key.json"))
data=d.get("data") if isinstance(d,dict) and "data" in d else d
print("tier", data.get("tier"), "limit", data.get("daily_limit"))
key=data.get("api_key","")
open("/tmp/skill_key.txt","w").write(key)
print("key_prefix", (key or "")[:12]+"...")
PY

KEY=$(cat /tmp/skill_key.txt)
curl -sS -m 20 -H "X-API-Key: $KEY" -o /tmp/sk.json -w "skills=%{http_code}\n" http://127.0.0.1:8000/api/skills
python3 - <<'PY'
import json
d=json.load(open("/tmp/sk.json"))
data=d.get("data") if isinstance(d,dict) and "data" in d else d
print("tier", data.get("tier"), "skills", len(data.get("skills") or []), "blocked", len(data.get("blocked") or []))
PY

# run a skill
curl -sS -m 30 -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"args":{"symbol":"600519","market":"CN"}}' \
  -o /tmp/run.json -w "run=%{http_code}\n" \
  http://127.0.0.1:8000/api/skills/get_stock_quote/run
python3 - <<'PY'
import json
d=json.load(open("/tmp/run.json"))
data=d.get("data") if isinstance(d,dict) and "data" in d else d
print("result_head", (data.get("result") or "")[:120])
print("risk", (data.get("risk") or "")[:60])
PY

# usage
curl -sS -m 15 -H "X-API-Key: $KEY" -o /tmp/usage.json -w "usage=%{http_code}\n" http://127.0.0.1:8000/api/usage
python3 -c "import json;d=json.load(open('/tmp/usage.json'));print(d.get('data',d))"
echo GATEWAY_SMOKE_OK
