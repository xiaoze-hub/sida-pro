#!/bin/bash
TOKEN=$(curl -s -X POST http://100.91.30.35:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"${SIDA_TEST_USER:-admin}\",\"password\":\"${SIDA_TEST_PASSWORD:?请先 export SIDA_TEST_PASSWORD=<测试口令>}\"}" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['token'])")

echo "=== breadth-distribution ==="
curl -s -o /dev/null -w "HTTP:%{http_code} time:%{time_total}s" -H "Authorization: Bearer $TOKEN" \
  "http://100.91.30.35:8000/api/market-data/breadth-distribution"
echo

echo "=== klines summary ==="
curl -s -o /dev/null -w "HTTP:%{http_code} time:%{time_total}s" -H "Authorization: Bearer $TOKEN" \
  "http://100.91.30.35:8000/api/klines/000001/summary?market=CN"
echo
