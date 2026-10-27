#!/bin/bash
curl -sS -o /dev/null -w "health=%{http_code} time=%{time_total}\n" http://127.0.0.1:8000/api/health
curl -sS -o /dev/null -w "index=%{http_code} time=%{time_total}\n" http://127.0.0.1:8000/
docker ps --filter name=sida --format "{{.Status}}"
docker logs sida --tail 15 2>&1
