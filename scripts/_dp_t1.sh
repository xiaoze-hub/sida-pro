#!/bin/bash
cd /mnt/c/Users/tianxiang/sida-work
git add frontend/src/pages/Terms.tsx frontend/src/components/Disclaimer.tsx frontend/src/App.tsx frontend/src/pages/Login.tsx frontend/src/pages/Developers.tsx src/core/alerting.py src/core/db_backup_auto.py src/core/ai_client.py src/core/datasource_failures.py src/core/md_metrics_sink.py src/web/middleware.py src/web/api/health.py src/bootstrap/runtime.py scripts/backup_auto.py scripts/restore_backup.py tests/test_alerting.py tests/test_backup_auto.py .env.example CHANGELOG.md
git commit -m "feat(tier1): 合规+告警+备份

- 用户协议/隐私政策页 /terms, 注册必勾
- 全局免责声明底部条(可关闭)
- 开发者页数据来源口径标注
- 告警体系: 5xx/磁盘/LLM429/DB/数据源 → 企业微信
- 备份自动化: 每日3点 pg_dump + 30天清理 + 恢复脚本"
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_git2_sidapro -o IdentitiesOnly=yes -o ConnectTimeout=20' timeout 90 git push origin main

cd /root/sida-pro
git pull --rebase origin main

# 复制后端文件
for f in src/core/alerting.py src/core/db_backup_auto.py src/core/ai_client.py src/core/datasource_failures.py src/core/md_metrics_sink.py src/web/middleware.py src/web/api/health.py src/bootstrap/runtime.py; do
  docker cp "/mnt/c/Users/tianxiang/sida-work/$f" "panwatch:/app/$f" 2>/dev/null
done

# 复制前端
rm -rf static
cp -r /mnt/c/Users/tianxiang/sida-work/frontend/dist static
docker cp static/. panwatch:/app/static/

docker restart panwatch
sleep 20
curl -s http://localhost:8000/api/version
echo
docker inspect --format='{{.State.Health.Status}}' panwatch
echo "TIER1_DONE"
