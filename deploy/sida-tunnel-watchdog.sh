#!/bin/bash
# SIDA 反向隧道看门狗（2026-10-21 502 事故后固化，装到小主机 WSL）
# 事故链：docker 重启 → 8000 短暂不可用 → ssh -R 建好转发但目标已死；
#         旧 sshd 监听滞留 18000 → 新转发 ExitOnForwardFailure 直接退出 → 外网 502。
# 部署：sudo cp deploy/sida-tunnel-watchdog.sh /usr/local/bin/ && sudo chmod +x
#       systemd timer 每 2 分钟跑一次（见 deploy/sida-tunnel-watchdog.timer）
set -u
KEY=/root/.ssh/id_ed25519_revtunnel
CLOUD=root@101.35.244.238
SSH="ssh -i $KEY -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"

code=$($SSH $CLOUD "curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:18000/api/health" 2>/dev/null)
[ "$code" = "200" ] && exit 0

logger -t sida-tunnel-wd "health=$code -> restart tunnel"
systemctl restart sida-reverse-tunnel
sleep 8
code=$($SSH $CLOUD "curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:18000/api/health" 2>/dev/null)
[ "$code" = "200" ] && { logger -t sida-tunnel-wd "recovered after restart"; exit 0; }

pid=$($SSH $CLOUD "ss -lntp 2>/dev/null | grep ':18000' | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2" 2>/dev/null)
if [ -n "${pid:-}" ]; then
  logger -t sida-tunnel-wd "kill stale listener pid=$pid"
  $SSH $CLOUD "kill $pid" >/dev/null 2>&1
  sleep 3
fi
systemctl restart sida-reverse-tunnel
sleep 8
code=$($SSH $CLOUD "curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:18000/api/health" 2>/dev/null)
logger -t sida-tunnel-wd "final health=$code"
