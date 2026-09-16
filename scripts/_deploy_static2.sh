#!/bin/bash
set -euo pipefail
# 从 Windows dist 同步到 WSL 仓库再 docker cp
SRC=/mnt/c/Users/tianxiang/sida-work/frontend/dist
DEST=/root/sida-pro/frontend/dist
CT=panwatch
echo "sync $SRC -> $DEST"
rm -rf "$DEST"
mkdir -p "$DEST"
cp -a "$SRC/." "$DEST/"
grep -oE 'index-[A-Za-z0-9_-]+\.js' "$DEST/index.html"
docker cp "$DEST/." "$CT:/app/static/"
docker exec -u root "$CT" sh -c 'echo v0.6.5 > /app/VERSION && chown -R app:app /app/static /app/VERSION'
curl -sS http://127.0.0.1:8000/ | grep -oE 'index-[A-Za-z0-9_-]+\.js' | head -3
curl -sS http://127.0.0.1:8000/api/version; echo
echo STATIC_OK
