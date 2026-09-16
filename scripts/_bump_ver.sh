#!/bin/bash
set -euo pipefail
CT=panwatch
docker exec -u root "$CT" sh -c 'printf v0.6.6 > /app/VERSION && chown app:app /app/VERSION && cat /app/VERSION'
curl -sS http://127.0.0.1:8000/api/version; echo
curl -sS http://127.0.0.1:8000/api/health | python3 -c "import sys,json;d=json.load(sys.stdin);print('health',d.get('data',{}).get('status'),d.get('data',{}).get('version'))"
curl -sS http://127.0.0.1:8000/ | grep -oE 'index-[A-Za-z0-9_-]+\.js' | head -2
echo VERSION_OK
