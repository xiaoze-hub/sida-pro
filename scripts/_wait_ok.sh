#!/bin/bash
for i in $(seq 1 30); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' panwatch 2>/dev/null || echo unknown)
  echo "[$i] $ST"
  if [ "$ST" = "healthy" ]; then break; fi
  sleep 5
done
curl -sS http://127.0.0.1:8000/api/version; echo
curl -sS -o /dev/null -w "health=%{http_code}\n" http://127.0.0.1:8000/api/health
echo DONE
