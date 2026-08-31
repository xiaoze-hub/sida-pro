#!/usr/bin/env bash
# SIDA 生产 PostgreSQL 每日备份 (2026-08-23 Q4)
#
# 用法(服务器 root crontab 示例, 每日 23:30):
#   30 23 * * * /root/sida/scripts/backup_pg.sh >> /var/log/sida_pg_backup.log 2>&1
#
# 环境变量:
#   PGDATABASE / PGUSER / PGPASSWORD / PGHOST / PGPORT  (缺省见下)
#   BACKUP_DIR   备份目录(默认 /root/sida_backups)
#   BACKUP_KEEP  保留份数(默认 7)
set -euo pipefail

export PGHOST="${PGHOST:-127.0.0.1}"
export PGPORT="${PGPORT:-5432}"
export PGDATABASE="${PGDATABASE:-sida}"
export PGUSER="${PGUSER:-sida}"
export PGPASSWORD="${PGPASSWORD:?请设置 PGPASSWORD 或环境导出}"

BACKUP_DIR="${BACKUP_DIR:-/root/sida_backups}"
BACKUP_KEEP="${BACKUP_KEEP:-7}"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="${BACKUP_DIR}/sida_${STAMP}.dump"

mkdir -p "${BACKUP_DIR}"

# 逻辑备份(自定义格式, 可 pg_restore 单表恢复)
pg_dump -Fc -v -f "${OUT}" "${PGDATABASE}"

# 校验: dump 头必须是 PGDMP
if ! head -c 5 "${OUT}" | grep -q "PGDMP"; then
    echo "备份校验失败: ${OUT} 非 PGDMP 格式" >&2
    exit 1
fi

gzip -f "${OUT}"

# 滚动保留
ls -1t "${BACKUP_DIR}"/sida_*.dump.gz 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))" | xargs -r rm -f

echo "PG 备份完成: ${OUT}.gz ($(du -h "${OUT}.gz" | cut -f1))"
