#!/bin/bash
docker exec panwatch sh -c "ps aux | head -30"
echo "=== top cpu ==="
docker top panwatch -o pid,pcpu,pmem,cmd | head -20
echo "=== recent logs warn/error ==="
docker logs panwatch --since 2m 2>&1 | grep -iE "error|exception|traceback|died|WARNING src" | tail -30
