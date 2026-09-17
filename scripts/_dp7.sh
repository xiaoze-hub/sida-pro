#!/bin/bash
cd /mnt/c/Users/tianxiang/sida-work
git add frontend/src/pages/ApiKeys.tsx
git commit -m "fix(api-keys): 修复 o.map is not a function 崩溃

- keys 响应可能不是纯数组, 加 Array.isArray 保护
- 兼容 {data:[...]} / {keys:[...]} 包装格式
- keyList 始终返回 MyApiKey[] 数组"
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_git2_sidapro -o IdentitiesOnly=yes -o ConnectTimeout=20' timeout 90 git push origin main

cd /root/sida-pro
git pull --rebase origin main
rm -rf static
cp -r /mnt/c/Users/tianxiang/sida-work/frontend/dist static
docker cp static/. panwatch:/app/static/
docker restart panwatch
sleep 20
curl -s http://localhost:8000/api/version
echo
docker inspect --format='{{.State.Health.Status}}' panwatch
echo "DONE"
