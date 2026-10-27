#!/bin/bash
docker logs sida --tail 50 2>&1
echo "=== ps ==="
docker ps -a --filter name=sida --format "{{.Status}}"
