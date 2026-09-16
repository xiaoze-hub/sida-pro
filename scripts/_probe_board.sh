#!/bin/bash
TOKEN=$(cat /mnt/c/Users/tianxiang/sida-work/data/admin_token.txt | tr -d '\r\n')
# heat map boards
curl -sS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000/api/boards/heatmap?live=auto" -o /tmp/hm.json
python3 - <<'PY'
import json
d=json.load(open("/tmp/hm.json"))
data=d.get("data") if isinstance(d,dict) and "data" in d else d
items = (data or {}).get("items") or (data or {}).get("boards") or []
print("n", len(items))
if items:
  print("sample", {k:items[0].get(k) for k in list(items[0])[:8]})
  codes=[i.get("block_code") or i.get("code") for i in items[:3]]
  print("codes", codes)
PY
# test one board
CODE=$(python3 -c "import json;d=json.load(open('/tmp/hm.json'));data=d.get('data',d);items=data.get('items') or data.get('boards') or [];print((items[0].get('block_code') or items[0].get('code')) if items else '')")
echo "CODE=$CODE"
if [ -n "$CODE" ]; then
  curl -sS -H "Authorization: Bearer $TOKEN" -w " board=%{http_code}\n" -o /tmp/bd.json "http://127.0.0.1:8000/api/boards/$CODE"
  head -c 200 /tmp/bd.json; echo
fi
