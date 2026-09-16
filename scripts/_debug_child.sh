#!/bin/bash
# 找 child 崩溃真实原因
docker exec -u app panwatch python -c "
import sys
sys.path.insert(0, '/app')
from src.bootstrap.app import create_app
app = create_app()
print('APP_OK')
" 2>&1 | tail -40
echo "=== try import server ==="
docker exec -u app panwatch python -c "
import sys
sys.path.insert(0,'/app')
import server
print('SERVER_IMPORT_OK')
" 2>&1 | tail -20
echo "=== resource ==="
docker stats panwatch --no-stream --format "{{.CPUPerc}} {{.MemUsage}}"
