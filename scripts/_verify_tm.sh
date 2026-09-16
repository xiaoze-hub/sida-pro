#!/bin/bash
set -uo pipefail
CT=panwatch
for i in $(seq 1 24); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' "$CT" 2>/dev/null || echo unknown)
  echo "[$i] $ST"
  [ "$ST" = "healthy" ] && break
  sleep 5
done
TOKEN=$(cat /mnt/c/Users/tianxiang/sida-work/data/admin_token.txt | tr -d '\r\n')
echo "=== board ==="
curl -sS -o /tmp/tm.json -H "Authorization: Bearer $TOKEN" -w "http=%{http_code} time=%{time_total}\n" \
  "http://127.0.0.1:8000/api/theme-mood/board?window=20&top=15"
python3 - <<'PY'
import json
d=json.load(open("/tmp/tm.json"))
data=d.get("data") if isinstance(d,dict) and "data" in d else d
print("keys", list(data.keys())[:10] if isinstance(data,dict) else type(data))
if isinstance(data,dict):
  items=data.get("items") or data.get("boards") or []
  print("items", len(items) if hasattr(items,"__len__") else items)
  if items: print("first", str(items[0])[:180])
PY
echo "=== ladder ==="
curl -sS -o /tmp/ld.json -H "Authorization: Bearer $TOKEN" -w "http=%{http_code} time=%{time_total}\n" \
  "http://127.0.0.1:8000/api/theme-mood/ladder?window=20"
python3 - <<'PY'
import json
d=json.load(open("/tmp/ld.json"))
data=d.get("data") if isinstance(d,dict) and "data" in d else d
if isinstance(data,dict):
  print("keys", list(data.keys())[:10])
  print("ladder", len(data.get("ladder") or []), "mode", data.get("mode"), "stale", data.get("stale"))
else:
  print(str(data)[:200])
PY
