#!/bin/bash
cd /mnt/c/Users/tianxiang/sida-work
git add frontend/src/pages/Admin.tsx frontend/src/App.tsx src/web/api/users.py src/web/api/skills_gateway.py
git commit -m "feat(admin): 管理后台(用户管理/Key管理/用量监控/Pro审核)

- 后端: GET /users/admin/list, toggle-active, change-role, stats
- 前端: /admin页面, DevPageLayout, 4区块
- owner双重防护: PermGuard + require_owner"
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_git2_sidapro -o IdentitiesOnly=yes -o ConnectTimeout=20' timeout 90 git push origin main

cd /root/sida-pro
git pull --rebase origin main
docker cp /mnt/c/Users/tianxiang/sida-work/src/web/api/users.py panwatch:/app/src/web/api/users.py
docker cp /mnt/c/Users/tianxiang/sida-work/src/web/api/skills_gateway.py panwatch:/app/src/web/api/skills_gateway.py

rm -rf static
cp -r /mnt/c/Users/tianxiang/sida-work/frontend/dist static
docker cp static/. panwatch:/app/static/

docker restart panwatch
sleep 20
curl -s http://localhost:8000/api/version
echo
docker inspect --format='{{.State.Health.Status}}' panwatch
echo "DONE"
