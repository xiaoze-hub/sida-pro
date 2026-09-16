#!/bin/bash
TOKEN=$(cat /mnt/c/Users/tianxiang/sida-work/data/admin_token.txt | tr -d '\r\n')
for p in \
  "/api/theme-mood/board?window=20&top=15" \
  "/api/theme-mood/ladder?window=20" \
  "/api/theme-mood/board" \
  "/api/theme-mood/ladder"
do
  echo "==== $p"
  curl -sS -o /tmp/tm.json -H "Authorization: Bearer $TOKEN" \
    -w "http=%{http_code} time=%{time_total}\n" \
    "http://127.0.0.1:8000$p"
  python3 - <<'PY'
import json
try:
  d=json.load(open("/tmp/tm.json"))
  data=d.get("data") if isinstance(d,dict) and "data" in d else d
  if isinstance(data,dict):
    print("keys", list(data.keys())[:12])
    for k in ("boards","items","ladder","count","note","stale","as_of"):
      if k in data:
        v=data[k]
        print(k, type(v).__name__, (len(v) if hasattr(v,"__len__") else v))
  elif isinstance(data,list):
    print("list len", len(data), "first", str(data[:1])[:200])
  else:
    print(str(data)[:200])
except Exception as e:
  print("raw", open("/tmp/tm.json").read()[:300], e)
PY
done
