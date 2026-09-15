#!/bin/bash
# v0.7.0 镜像构建
set -euo pipefail
cd /root/sida-pro
git fetch origin --tags
git checkout main
git pull --rebase origin main
echo "HEAD=$(git rev-parse --short HEAD) VERSION=$(cat VERSION)"

# 同步 Windows 侧已构建的 frontend dist
if [ ! -f frontend/dist/index.html ]; then
  cp -r /mnt/c/Users/tianxiang/sida-work/frontend/dist frontend/dist 2>/dev/null || true
fi
rm -rf static && mkdir -p static && cp -a frontend/dist/. static/

# ghcr login (thsdk-vendor)
TOKEN=$(grep -oE 'ghp_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+' /root/.git-credentials | head -1)
echo "$TOKEN" | docker login ghcr.io -u xiaoze-hub --password-stdin

echo "START $(date +%T)"
docker build --platform linux/amd64 \
  --build-arg VERSION=v0.7.0 \
  -t xiaoze-hub/stock-intelligent-data-analytics:v0.7.0 \
  -t xiaoze-hub/stock-intelligent-data-analytics:latest \
  -t panwatch:v0.7.0 \
  . 2>&1 | tail -30
echo "BUILD_EXIT=$?"
docker images | grep -E "v0.7.0|panwatch" | head -5
echo BUILD_DONE
