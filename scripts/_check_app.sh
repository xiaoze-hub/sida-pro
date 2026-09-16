#!/bin/bash
curl -sS -o /dev/null -w "health=%{http_code} time=%{time_total}\n" http://127.0.0.1:8000/api/health
curl -sS -o /dev/null -w "index=%{http_code} time=%{time_total}\n" http://127.0.0.1:8000/
docker ps --filter name=panwatch --format "{{.Status}}"
docker logs panwatch --tail 15 2>&1
