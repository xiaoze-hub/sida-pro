#!/bin/bash
docker exec sida sh -c "ps aux | head -30"
echo "=== top cpu ==="
docker top sida -o pid,pcpu,pmem,cmd | head -20
echo "=== recent logs warn/error ==="
docker logs sida --since 2m 2>&1 | grep -iE "error|exception|traceback|died|WARNING src" | tail -30
