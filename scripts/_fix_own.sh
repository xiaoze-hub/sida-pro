#!/bin/bash
docker exec -u app sida python -c "
import sys
sys.path.insert(0,'/app')
try:
    from src.bootstrap.app import create_app
    print('create_app ok')
except Exception:
    import traceback; traceback.print_exc()
" 2>&1 | tail -40
echo "=== chown /app ==="
docker exec -u root sida chown -R app:app /app/src /app/server.py /app/static 2>&1 | tail -5
docker exec -u app sida python -m compileall -q /app/src /app/server.py 2>&1 | tail -5
docker restart sida
sleep 20
docker logs sida --tail 30 2>&1
docker inspect sida --format '{{.State.Health.Status}}'
