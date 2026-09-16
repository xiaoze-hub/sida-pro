#!/bin/bash
docker logs panwatch --tail 50 2>&1
echo "=== ps ==="
docker ps -a --filter name=panwatch --format "{{.Status}}"
