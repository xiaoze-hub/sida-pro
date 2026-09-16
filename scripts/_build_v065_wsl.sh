#!/bin/bash
set -e
cd /root/sida-pro
if [ ! -d static ] || [ -z "$(ls -A static 2>/dev/null)" ]; then
  rm -rf static && mkdir -p static && cp -r frontend/dist/* static/
fi
docker build --progress=plain --platform linux/amd64 \
  --build-arg VERSION=v0.6.5 \
  --build-arg THSDK_IMAGE=local/thsdk-vendor:v1.7.18 \
  -t xiaoze-hub/stock-intelligent-data-analytics:v0.6.5 \
  -t xiaoze-hub/stock-intelligent-data-analytics:latest \
  .
echo BUILD_OK
docker images | grep stock-intelligent
