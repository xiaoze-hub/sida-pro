#!/bin/bash
echo "=== status ==="
docker ps -a --filter name=panwatch --format "{{.Status}}"
docker logs panwatch --tail 80 2>&1
echo "=== env count ==="
docker inspect panwatch --format '{{range .Config.Env}}{{println .}}{{end}}' | wc -l
docker inspect panwatch --format '{{range .Config.Env}}{{println .}}{{end}}' | sed 's/\(PASSWORD\|TOKEN\|SECRET\|KEY\)=.*/\1=***/' | head -20
echo "=== health log ==="
docker inspect panwatch --format '{{json .State.Health}}' | python3 -c "import sys,json; h=json.load(sys.stdin); print(h.get('Status')); [print(x.get('End'), x.get('ExitCode'), (x.get('Output') or '')[-200:]) for x in (h.get('Log') or [])[-3:]]"
