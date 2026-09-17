#!/bin/bash
TOKEN=$(curl -s -X POST http://100.91.30.35:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"xz.170530"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['token'])")

echo "=== /api/datasources/capabilities ==="
curl -s -w "\nHTTP:%{http_code}\n" -H "Authorization: Bearer $TOKEN" \
  http://100.91.30.35:8000/api/datasources/capabilities | tail -3

echo "=== /api/market/phase ==="
curl -s -w "\nHTTP:%{http_code}\n" -H "Authorization: Bearer $TOKEN" \
  http://100.91.30.35:8000/api/market/phase | tail -3

echo "=== /api/resonance/scan ==="
curl -s -w "\nHTTP:%{http_code}\n" -H "Authorization: Bearer $TOKEN" \
  "http://100.91.30.35:8000/api/resonance/scan?only=resonance&limit=30" | tail -3

echo "=== /api/market-data/breadth-distribution ==="
curl -s -w "\nHTTP:%{http_code}\n" -H "Authorization: Bearer $TOKEN" \
  http://100.91.30.35:8000/api/market-data/breadth-distribution | tail -3
