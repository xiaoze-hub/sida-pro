#!/bin/bash
cd /mnt/c/Users/tianxiang/sida-work
git checkout main
git merge --no-ff feat/admin-console-20260916 -m "merge: Admin管理后台"
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
