#!/bin/bash
TOKEN=$(cat /mnt/c/Users/tianxiang/sida-work/data/admin_token.txt | tr -d '\r\n')
for i in 1 2 3 4 5; do
  curl -sS -o /tmp/ph.json -H "Authorization: Bearer $TOKEN" -w "try$i http=%{http_code} time=%{time_total}\n" http://127.0.0.1:8000/api/market/phase
done
python3 - <<'PY'
import json
raw=open("/tmp/ph.json").read()
print(raw[:400])
try:
  d=json.loads(raw)
  data=d.get("data") if isinstance(d,dict) and "data" in d else d
  if isinstance(data,dict):
    print("available", data.get("available"), "note", (data.get("note") or "")[:100])
    print("current", data.get("current"))
    print("recent", len(data.get("recent_30d") or data.get("recent_days") or []))
except Exception as e:
  print("parse err", e)
PY
