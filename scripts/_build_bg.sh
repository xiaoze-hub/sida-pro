#!/bin/bash
set -euo pipefail
cd /root/sida-pro
TOKEN=$(grep -oE 'ghp_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+' /root/.git-credentials | head -1)
echo "$TOKEN" | docker login ghcr.io -u xiaoze-hub --password-stdin >/dev/null
echo "START $(date +%F_%T)" | tee -a /tmp/build_v066.log
docker build --platform linux/amd64 \
  --build-arg VERSION=v0.6.6 \
  -t xiaoze-hub/stock-intelligent-data-analytics:v0.6.6 \
  -t xiaoze-hub/stock-intelligent-data-analytics:latest \
  -t panwatch:v0.6.6 \
  . >> /tmp/build_v066.log 2>&1
echo "DOCKER_BUILD_EXIT=$?" | tee -a /tmp/build_v066.log
docker images | grep -E "v0.6.6" | tee -a /tmp/build_v066.log
echo BUILD_BG_DONE | tee -a /tmp/build_v066.log
