#!/bin/bash
set -euo pipefail
CT=panwatch
DIST=/root/sida-pro/frontend/dist
echo "=== static deploy (no restart) ==="
ls "$DIST/index.html"
# 清空再拷? CHANGELOG 说保留历史 chunk 便于回滚 index.html; 先拷贝覆盖
docker cp "$DIST/." "$CT:/app/static/"
docker exec -u root "$CT" sh -c 'echo v0.6.5 > /app/VERSION'
docker exec -u root "$CT" chown -R app:app /app/static /app/VERSION
echo "=== verify ==="
curl -sS http://127.0.0.1:8000/api/version
echo
curl -sS http://127.0.0.1:8000/ | grep -o 'index-[^"]*\.js' | head -3
echo STATIC_OK
