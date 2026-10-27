#!/usr/bin/env bash
# 在小主机 WSL 里部署 tdx-gateway（幂等）。用法: bash tdx_gateway/deploy.sh [tag]
set -euo pipefail

TAG="${1:-1.0.0}"
NAME=tdx-gateway
NET=sida-net
UPSTREAM="${TDX_UPSTREAM:-http://172.27.16.1:17709/}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== 1/4 上游连通性 =="
python3 - "$UPSTREAM" <<'PY'
import json, sys, urllib.request
url = sys.argv[1]
body = {"id": 1, "method": "get_market_snapshot", "params": {"stock_code": "600111.SH"}}
try:
    r = urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}), timeout=8)
    res = json.loads(r.read()).get("result", {})
    print("  upstream ErrorId=", res.get("ErrorId"), "Now=", res.get("Now"))
except Exception as e:
    print("  上游不可达:", type(e).__name__, e)
    print("  → 先确认 TdxW.exe 在跑，且 portproxy 存在:")
    print("     netsh interface portproxy show all   # 需要 5100 或 17709 → 127.0.0.1:17709")
    sys.exit(0)
PY

echo "== 2/4 构建 $NAME:$TAG =="
docker build -q -t "$NAME:$TAG" "$HERE"

echo "== 3/4 重启容器 =="
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --restart unless-stopped --network "$NET" \
  -p 127.0.0.1:17710:8000 \
  -e TDX_UPSTREAM="$UPSTREAM" \
  -e TDX_CONCURRENCY="${TDX_CONCURRENCY:-1}" \
  "$NAME:$TAG" >/dev/null

echo "== 4/4 自检 =="
for i in $(seq 1 20); do
  if curl -sf -m 3 http://127.0.0.1:17710/health >/dev/null 2>&1; then
    curl -s http://127.0.0.1:17710/health; echo; echo "OK (localhost:17710)"; exit 0
  fi
  sleep 2
done
echo "自检超时，看日志: docker logs --tail 40 $NAME"; exit 1
