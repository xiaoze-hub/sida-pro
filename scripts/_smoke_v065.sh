#!/bin/bash
set -uo pipefail
echo "=== container ==="
docker ps --filter name=panwatch --format "{{.Names}} | {{.Image}} | {{.Status}}"
docker exec panwatch cat /app/VERSION
echo
echo "=== /api/version ==="
curl -sS -w "\nhttp=%{http_code}\n" http://127.0.0.1:8000/api/version
echo
echo "=== /api/health ==="
curl -sS -o /tmp/h.json -w "http=%{http_code}\n" http://127.0.0.1:8000/api/health
python3 - <<'PY'
import json
d=json.load(open("/tmp/h.json"))
data=d.get("data") or {}
print("status=", data.get("status"), "version=", data.get("version"))
comps=data.get("components") or {}
for k,v in comps.items():
    if isinstance(v, dict):
        print(f"  {k}: {v.get('status')}")
PY
echo
echo "=== homepage ==="
curl -sS -o /tmp/idx.html -w "http=%{http_code} size=%{size_download}\n" http://127.0.0.1:8000/
grep -o 'index-[^"]*\.js' /tmp/idx.html | head -3
echo
echo "=== kline route (auth may 401) ==="
curl -sS -o /tmp/k.json -w "http=%{http_code}\n" "http://127.0.0.1:8000/api/klines/600519?market=CN&days=5"
head -c 200 /tmp/k.json; echo
echo
echo "=== thsdk breaker presence (v0.6.5 backend change) ==="
docker exec panwatch python -c "from src.core.thsdk_breaker import call_with_hard_timeout, breaker_status; print('breaker ok', breaker_status())"
echo SMOKE_DONE
