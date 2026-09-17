#!/bin/bash
cd /mnt/c/Users/tianxiang/sida-work
git add -A ':!scripts/*.png' ':!docs/screenshots' ':!scripts/.pw' ':!shots' ':!data'
git commit -m "feat(p0p1p2p3): 全维度加固(安全/稳定/体验/代码质量)

P0安全: JWT httpOnly Cookie双轨/HSTS/密钥90天自动轮换
P1稳定: Loki日志/APM追踪/慢接口异步化/读写分离准备
P2体验: i18n框架/Card组件库/移动端优化/骨架屏
P3+代码: GDPR数据导出删除/超长函数拆分/循环依赖解耦

- 34项测试通过
- 前端tsc通过
- 迁移173(users软删除)"
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_git2_sidapro -o IdentitiesOnly=yes -o ConnectTimeout=20' timeout 90 git push origin main

cd /root/sida-pro
git pull --rebase origin main

# 复制后端核心文件
for f in \
  src/core/auth_tokens.py src/core/secret_rotation.py src/core/permissions.py \
  src/core/loki_logger.py src/core/apm.py src/core/ai_client.py \
  src/core/entry_candidates.py src/core/strategy_engine.py \
  src/db/dialect.py src/db/session.py src/db/models.py \
  src/web/api/auth.py src/web/api/email_verify.py src/web/api/admin_secrets.py \
  src/web/api/user_data.py src/web/api/market_data.py src/web/api/settings.py \
  src/web/middleware.py src/web/app.py src/web/database.py src/web/migrations.py \
  src/bootstrap/runtime.py src/bootstrap/env.py src/bootstrap/startup.py \
  src/agents/premarket_outlook.py; do
  docker cp "/mnt/c/Users/tianxiang/sida-work/$f" "panwatch:/app/$f" 2>/dev/null
done

# 复制前端
rm -rf static
cp -r /mnt/c/Users/tianxiang/sida-work/frontend/dist static
docker cp static/. panwatch:/app/static/

docker restart panwatch
sleep 25
curl -s http://localhost:8000/api/version
echo
docker inspect --format='{{.State.Health.Status}}' panwatch
echo "ALL_DONE"
