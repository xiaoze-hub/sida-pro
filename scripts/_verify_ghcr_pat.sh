#!/bin/bash
# 验证 git-credentials 里的 PAT 是否能拉私有 thsdk-vendor (不打印完整 token)
set -uo pipefail
TOKEN=$(grep -oE 'ghp_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+' /root/.git-credentials | head -1)
if [ -z "$TOKEN" ]; then
  echo "NO_TOKEN_IN_GIT_CREDENTIALS"
  exit 1
fi
echo "TOKEN_PREFIX=${TOKEN:0:8}... len=${#TOKEN}"

# 1) user API + scopes
curl -sS -D /tmp/h.txt -o /tmp/u.json -H "Authorization: Bearer $TOKEN" https://api.github.com/user
echo "user_http=$(head -1 /tmp/h.txt | awk '{print $2}')"
grep -i x-oauth-scopes /tmp/h.txt | tr -d '\r'
python3 - <<'PY'
import json
try:
  u=json.load(open("/tmp/u.json"))
  print("login=", u.get("login"))
except Exception as e:
  print("user parse fail", e)
PY

# 2) ghcr token for private package
echo "=== ghcr token ==="
curl -sS -o /tmp/ghcr.json -w "ghcr_token_http=%{http_code}\n" \
  -u "xiaoze-hub:$TOKEN" \
  "https://ghcr.io/token?scope=repository:xiaoze-hub/thsdk-vendor:pull&service=ghcr.io"
# 3) docker login + pull
echo "$TOKEN" | docker login ghcr.io -u xiaoze-hub --password-stdin 2>&1 | tail -2
docker pull ghcr.io/xiaoze-hub/thsdk-vendor:v1.7.18 2>&1 | tail -5
echo PULL_EXIT=$?
