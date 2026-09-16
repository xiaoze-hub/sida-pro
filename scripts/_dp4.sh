#!/bin/bash
cd /mnt/c/Users/tianxiang/sida-work
git add frontend/src/pages/Landing.tsx
git commit -m "feat(landing): Harness排版+深海粒子+玻璃UI+ASCII原子"
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
