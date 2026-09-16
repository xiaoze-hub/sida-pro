#!/bin/bash
set -euo pipefail
cd /root/sida-pro
git fetch origin --tags
git checkout main
git pull --rebase origin main
echo "HEAD=$(git rev-parse --short HEAD)" > /tmp/build_v069.log
# frontend dist already in place or copy
if [ ! -f frontend/dist/index.html ]; then
  cp -r /mnt/c/Users/tianxiang/sida-work/frontend/dist frontend/dist 2>/dev/null || true
fi
rm -rf static && mkdir -p static && cp -a frontend/dist/. static/
TOKEN=$(grep -oE 'ghp_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+' /root/.git-credentials | head -1)
echo "$TOKEN" | docker login ghcr.io -u xiaoze-hub --password-stdin >> /tmp/build_v069.log 2>&1
echo "START $(date +%T)" >> /tmp/build_v069.log
docker build --platform linux/amd64 \
  --build-arg VERSION=v0.6.9 \
  -t xiaoze-hub/stock-intelligent-data-analytics:v0.6.9 \
  -t xiaoze-hub/stock-intelligent-data-analytics:latest \
  -t panwatch:v0.6.9 \
  . >> /tmp/build_v069.log 2>&1
echo "DOCKER_BUILD_EXIT=$?" >> /tmp/build_v069.log
docker images | grep v0.6.9 >> /tmp/build_v069.log
echo BUILD_BG_DONE >> /tmp/build_v069.log
