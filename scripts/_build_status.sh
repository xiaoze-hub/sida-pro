#!/bin/bash
echo "=== docker build procs ==="
ps aux | grep -E "docker build|buildkit" | grep -v grep | head
echo "=== last log ==="
tail -30 /tmp/build_v066.log 2>/dev/null || echo no_log
echo "=== images ==="
docker images | grep -E "v0.6.6|stock-intelligent|panwatch" | head -8
echo "=== disk ==="
df -h / | tail -1
