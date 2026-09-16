#!/bin/bash
# 完整镜像构建 v0.6.6 (PAT 已验证可拉 thsdk-vendor)
set -euo pipefail
cd /root/sida-pro
git fetch origin --tags
git checkout main
git pull --rebase origin main
echo "HEAD=$(git rev-parse --short HEAD) VERSION=$(cat VERSION)"

# 确保前端 dist 存在(Windows 侧已构建则同步)
if [ ! -f frontend/dist/index.html ]; then
  echo "build frontend in WSL"
  cd frontend && pnpm install --frozen-lockfile && pnpm build && cd ..
fi

# 打包 static 给 docker COPY
rm -rf static && mkdir -p static
cp -a frontend/dist/. static/

# 用已验证的 PAT 做 docker login (build 过程要 FROM ghcr thsdk-vendor)
TOKEN=$(grep -oE 'ghp_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+' /root/.git-credentials | head -1)
echo "$TOKEN" | docker login ghcr.io -u xiaoze-hub --password-stdin

echo "START_DOCKER_BUILD $(date +%T)"
docker build --platform linux/amd64 \
  --build-arg VERSION=v0.6.6 \
  -t xiaoze-hub/stock-intelligent-data-analytics:v0.6.6 \
  -t xiaoze-hub/stock-intelligent-data-analytics:latest \
  -t panwatch:v0.6.6 \
  . 2>&1 | tail -40
echo "BUILD_PIPE_EXIT=$?"
docker images | grep -E "v0.6.6|stock-intelligent" | head -5
echo BUILD_SCRIPT_DONE
