#!/bin/bash
cd /mnt/c/Users/tianxiang/sida-work
git add src/web/api/market_data.py
git commit -m "perf(breadth): 缓存TTL 60s→300s, 避免32s重算超时"
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_git2_sidapro -o IdentitiesOnly=yes -o ConnectTimeout=20' timeout 90 git push origin main

cd /root/sida-pro
git pull --rebase origin main
docker cp /mnt/c/Users/tianxiang/sida-work/src/web/api/market_data.py panwatch:/app/src/web/api/market_data.py
docker restart panwatch
sleep 20
curl -s http://localhost:8000/api/version
echo
docker inspect --format='{{.State.Health.Status}}' panwatch
echo "DONE"
