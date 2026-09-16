#!/bin/bash
set -euo pipefail
cd /root/sida-pro
git checkout main
git pull --rebase origin main
echo "HEAD=$(git rev-parse --short HEAD) VERSION=$(cat VERSION)" > /tmp/build_v067.log
# frontend dist from windows
if [ ! -f frontend/dist/index.html ]; then
  echo "NO DIST" >> /tmp/build_v067.log
  exit 1
fi
rm -rf static && mkdir -p static && cp -a frontend/dist/. static/
TOKEN=$(grep -oE 'ghp_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+' /root/.git-credentials | head -1)
echo "$TOKEN" | docker login ghcr.io -u xiaoze-hub --password-stdin >> /tmp/build_v067.log 2>&1
echo "START $(date +%T)" >> /tmp/build_v067.log
docker build --platform linux/amd64 \
  --build-arg VERSION=v0.6.7 \
  -t xiaoze-hub/stock-intelligent-data-analytics:v0.6.7 \
  -t xiaoze-hub/stock-intelligent-data-analytics:latest \
  -t panwatch:v0.6.7 \
  . >> /tmp/build_v067.log 2>&1
echo "DOCKER_BUILD_EXIT=$?" >> /tmp/build_v067.log
docker images | grep v0.6.7 >> /tmp/build_v067.log
echo BUILD_BG_DONE >> /tmp/build_v067.log
