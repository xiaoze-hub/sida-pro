#!/bin/bash
cd /mnt/c/Users/tianxiang/sida-work
git checkout main
git merge --no-ff feat/tier1-compliance-20260916 -m "merge: 梯队1 合规+告警+备份"
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_git2_sidapro -o IdentitiesOnly=yes -o ConnectTimeout=20' timeout 90 git push origin main

cd /root/sida-pro
git pull --rebase origin main

for f in src/core/alerting.py src/core/db_backup_auto.py src/core/ai_client.py src/core/datasource_failures.py src/core/md_metrics_sink.py src/web/middleware.py src/web/api/health.py src/bootstrap/runtime.py; do
  docker cp "/mnt/c/Users/tianxiang/sida-work/$f" "panwatch:/app/$f" 2>/dev/null
done

rm -rf static
cp -r /mnt/c/Users/tianxiang/sida-work/frontend/dist static
docker cp static/. panwatch:/app/static/

docker restart panwatch
sleep 20
curl -s http://localhost:8000/api/version
echo
docker inspect --format='{{.State.Health.Status}}' panwatch
echo "TIER1_MERGED"
