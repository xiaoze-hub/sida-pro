#!/bin/bash
cd /mnt/c/Users/tianxiang/sida-work
git add -A ':!scripts/*.png' ':!docs/screenshots' ':!scripts/.pw' ':!shots'
git commit -m "feat(tier2): CSP+CSRF+亮色主题+空态统一+依赖扫描

- CSP/安全头中间件(X-Frame-Options/Referrer-Policy等)
- CSRF双提交Cookie防护(与JWT兼容)
- 依赖漏洞扫描脚本+周扫GitHub Actions
- 亮色主题: Landing/Disclaimer/Terms/DevPageLayout
- 统一空态/错误态/骨架屏组件
- 接入7个关键页面"
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_git2_sidapro -o IdentitiesOnly=yes -o ConnectTimeout=20' timeout 90 git push origin main

cd /root/sida-pro
git pull --rebase origin main

# 复制后端
for f in src/web/middleware.py src/web/app.py src/web/api/auth.py; do
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
echo "TIER2_DONE"
