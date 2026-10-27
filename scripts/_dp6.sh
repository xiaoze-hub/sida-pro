#!/bin/bash
cd /mnt/c/Users/tianxiang/sida-work
git checkout main
git merge --no-ff feat/email-verify-20260916 -m "merge: 邮箱验证码注册+登录"
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_git2_sidapro -o IdentitiesOnly=yes -o ConnectTimeout=20' timeout 90 git push origin main

# 部署
cd /root/sida-pro
git pull --rebase origin main
docker cp /mnt/c/Users/tianxiang/sida-work/src/web/api/email_verify.py sida:/app/src/web/api/email_verify.py
docker cp /mnt/c/Users/tianxiang/sida-work/src/web/api/auth.py sida:/app/src/web/api/auth.py
docker cp /mnt/c/Users/tianxiang/sida-work/src/web/app.py sida:/app/src/web/app.py

rm -rf static
cp -r /mnt/c/Users/tianxiang/sida-work/frontend/dist static
docker cp static/. sida:/app/static/

docker restart sida
sleep 20
curl -s http://localhost:8000/api/version
echo
docker inspect --format='{{.State.Health.Status}}' sida
echo "DONE"
