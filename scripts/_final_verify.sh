#!/bin/bash
TOKEN=$(cat /mnt/c/Users/tianxiang/sida-work/data/admin_token.txt | tr -d '\r\n')
# remint token
cp /mnt/c/Users/tianxiang/sida-work/scripts/_mint_token.py /tmp/mint_token.py
docker cp /tmp/mint_token.py panwatch:/tmp/mint_token.py
docker exec -u app panwatch python /tmp/mint_token.py 2>/dev/null
docker exec panwatch cat /tmp/.admin_tok > /mnt/c/Users/tianxiang/sida-work/data/admin_token.txt
TOKEN=$(cat /mnt/c/Users/tianxiang/sida-work/data/admin_token.txt | tr -d '\r\n')
echo "=== board ==="
curl -sS -o /tmp/tm.json -H "Authorization: Bearer $TOKEN" -w "http=%{http_code}\n" \
  "http://127.0.0.1:8000/api/theme-mood/board?window=20&top=5"
head -c 200 /tmp/tm.json; echo
echo "=== ladder ==="
curl -sS -o /tmp/ld.json -H "Authorization: Bearer $TOKEN" -w "http=%{http_code} time=%{time_total}\n" \
  "http://127.0.0.1:8000/api/theme-mood/ladder?window=20"
python3 -c "import json;d=json.load(open('/tmp/ld.json'));print('ladder days',len(d.get('data',d).get('ladder') or []))"
echo "=== phase ==="
curl -sS -o /tmp/ph.json -H "Authorization: Bearer $TOKEN" -w "http=%{http_code}\n" \
  "http://127.0.0.1:8000/api/market/phase"
echo "=== homepage ==="
curl -sS -o /dev/null -w "index=%{http_code}\n" http://127.0.0.1:8000/
echo OK
