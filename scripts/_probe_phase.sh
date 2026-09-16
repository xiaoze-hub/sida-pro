#!/bin/bash
for i in 1 2 3 4 5; do
  curl -sS -o /tmp/ph.json -w "try$i http=%{http_code} time=%{time_total}\n" http://127.0.0.1:8000/api/market/phase
done
python3 - <<'PY'
import json
d=json.load(open("/tmp/ph.json"))
print("keys", list(d.keys())[:8])
print("available", d.get("available"), "current", d.get("current"), "note", (d.get("note") or "")[:80])
print("recent", len(d.get("recent_30d") or d.get("recent_days") or []))
PY
