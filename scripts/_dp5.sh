#!/bin/bash
cd /mnt/c/Users/tianxiang/sida-work
git add -A ':!scripts/*.png' ':!docs/screenshots' ':!scripts/.pw'
git commit -m "feat(auth): 邮箱验证码注册+验证码登录

- POST /api/auth/send-code: 发送验证码(5分钟过期, 每分钟1次)
- 注册强制验证码校验
- POST /api/auth/login-by-email: 邮箱+验证码登录
- 前端三模式Tab: 密码登录/注册/验证码登录
- 60秒发送倒计时
- SMTP未配置时开发模式打印验证码"
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_git2_sidapro -o IdentitiesOnly=yes -o ConnectTimeout=20' timeout 90 git push origin main
echo "DONE"
