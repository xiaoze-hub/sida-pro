#!/bin/bash
set -euo pipefail
cd /root/sida-pro
git fetch origin --tags
git checkout main
git pull --rebase origin main
echo "HEAD=$(git rev-parse --short HEAD) VERSION=$(cat VERSION)"

# 同步 Windows 侧已构建的 frontend dist
rm -rf frontend/dist static
cp -r /mnt/c/Users/tianxiang/sida-work/frontend/dist frontend/dist
mkdir -p static && cp -a frontend/dist/. static/

# ghcr login
TOKEN=$(grep -oE 'ghp_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+' /root/.git-credentials | head -1)
echo "$TOKEN" | docker login ghcr.io -u xiaoze-hub --password-stdin

# 用 Dockerfile.fast(跳过容器内前端构建)
cp /mnt/c/Users/tianxiang/sida-work/Dockerfile.fast . 2>/dev/null || true
if [ ! -f Dockerfile.fast ]; then
  echo "Dockerfile.fast 不存在, 用原 Dockerfile"
  docker build --platform linux/amd64 --build-arg VERSION=v0.7.1 -t panwatch:v0.7.1 . 2>&1 | tail -20
else
  # 需要 static/ 不被 dockerignore
  sed -i 's|^static/|# static/|' .dockerignore 2>/dev/null || true
  docker build --platform linux/amd64 --build-arg VERSION=v0.7.1 -f Dockerfile.fast -t panwatch:v0.7.1 . 2>&1 | tail -20
fi

echo "BUILD_EXIT=$?"
docker images | grep v0.7.1 | head -3
echo BUILD_DONE
