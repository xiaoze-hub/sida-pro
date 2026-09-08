#!/usr/bin/env bash
# SIDA 生产 PostgreSQL 每日备份 (2026-08-23 Q4; 2026-09-08 T11 适配 compose 拓扑)
#
# T11 修复: 原脚本默认 PGHOST=127.0.0.1, 但 compose 的 panwatch-postgres 没有
# 5432 端口映射 → 主机 cron 直连必然失败(且无人知晓)。现优先走
# `docker exec panwatch-postgres pg_dump`(与部署拓扑一致), 显式 TCP 兜底保留。
# 备份失败经 Hermes webhook 告警(与 alertmanager 同通道)。
#
# 用法(服务器 root crontab 示例, 每日 23:30):
#   30 23 * * * /root/sida/scripts/backup_pg.sh >> /var/log/sida_pg_backup.log 2>&1
#
# 环境变量:
#   PGCONTAINER   PG 容器名(默认 panwatch-postgres, docker exec 模式)
#   PGDATABASE / PGUSER / PGPASSWORD / PGHOST / PGPORT  (TCP 兜底模式用)
#   BACKUP_DIR    备份目录(默认 /root/sida_backups)
#   BACKUP_KEEP   保留份数(默认 7)
#   ALERT_WEBHOOK_URL  备份失败告警 webhook(默认 Hermes 桥 docker0 网关)
set -uo pipefail

PGCONTAINER="${PGCONTAINER:-panwatch-postgres}"
PGDATABASE="${PGDATABASE:-sida}"
PGUSER="${PGUSER:-sida}"
BACKUP_DIR="${BACKUP_DIR:-/root/sida_backups}"
BACKUP_KEEP="${BACKUP_KEEP:-7}"
ALERT_WEBHOOK_URL="${ALERT_WEBHOOK_URL:-http://172.17.0.1:8644/webhook/alertmanager}"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="${BACKUP_DIR}/sida_${STAMP}.dump"

alert() {
    # 备份失败必须叫人(静默的备份 = 没有备份)
    curl -sf -m 10 -X POST "${ALERT_WEBHOOK_URL}" \
        -H 'Content-Type: application/json' \
        -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"[SIDA] PG 备份失败: $1\"}}" \
        >/dev/null 2>&1 || true
    echo "备份失败: $1" >&2
}
trap 'alert "脚本异常退出(行 $?)"' ERR

mkdir -p "${BACKUP_DIR}"

if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "${PGCONTAINER}"; then
    # 模式 A(默认): compose 拓扑, PG 无主机端口映射, 走 docker exec
    # 密码从容器自身 env 取(postgres 镜像启动时已注入), 无需在宿主机保存
    docker exec "${PGCONTAINER}" pg_dump -U "${PGUSER}" -Fc "${PGDATABASE}" > "${OUT}"
else
    # 模式 B(兜底): 显式 TCP(非 compose 部署)
    export PGHOST="${PGHOST:-127.0.0.1}"
    export PGPORT="${PGPORT:-5432}"
    export PGPASSWORD="${PGPASSWORD:?TCP 模式请设置 PGPASSWORD}"
    pg_dump -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -Fc -f "${OUT}" "${PGDATABASE}"
fi

if [ ! -s "${OUT}" ]; then
    alert "dump 文件为空: ${OUT}"
    exit 1
fi

# 校验: dump 头必须是 PGDMP
if ! head -c 5 "${OUT}" | grep -q "PGDMP"; then
    alert "备份校验失败: ${OUT} 非 PGDMP 格式"
    exit 1
fi

gzip -f "${OUT}"

# 滚动保留
ls -1t "${BACKUP_DIR}"/sida_*.dump.gz 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))" | xargs -r rm -f

echo "PG 备份完成: ${OUT}.gz ($(du -h "${OUT}.gz" | cut -f1))"

# ── P4 (2026-09-07) 恢复演练(每月一次, 别等真挂了才练) ─────────────────
# 演练步骤(演练库, 绝不在生产库上 restore; compose 下用 docker exec 进容器做):
#   1) docker exec panwatch-postgres sh -c \
#        'createdb -U sida sida_drill && gunzip -c /tmp/sida_xxx.dump.gz > /tmp/x.dump && \
#         pg_restore -U sida -d sida_drill --clean /tmp/x.dump'
#      (先把 dump 拷进容器: docker cp sida_xxx.dump.gz panwatch-postgres:/tmp/)
#   2) 行数对账, 三张核心表必须 >0:
#      docker exec panwatch-postgres psql -U sida -d sida_drill -c \
#        "SELECT count(*) FROM klines; SELECT count(*) FROM users; SELECT count(*) FROM audit_logs;"
#   3) 对账通过 → dropdb sida_drill, 把行数记到 CHANGELOG/值班表。
#   4) 备份文件异地一份(对象存储/另一台机器各一, 生产铁律)。
#      无对象存储时的最低要求: crontab 里加一条 rsync 到另一台机器。
