#!/bin/sh
echo "VERSION:"
curl -s -m 10 http://localhost:8000/api/health | grep -o '"version":"[^"]*"'
echo "TRACEBACK_COUNT:"
docker logs panwatch 2>&1 | grep -ci traceback || echo 0
echo "STATUS:"
docker ps --filter name=panwatch --format "{{.Image}} {{.Status}}"
