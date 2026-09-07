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

# ── P4 (2026-09-07) 恢复演练(每月一次, 别等真挂了才练) ─────────────────
# 演练步骤(演练库, 绝不在生产库上 restore):
#   1) createdb sida_drill && pg_restore -d sida_drill --clean "${OUT}.gz 的解压前文件"
#      (注意: 先 gunzip -k, pg_restore 吃 .dump 不吃 .gz)
#   2) 行数对账, 三张核心表必须 >0:
#      SELECT count(*) FROM klines; SELECT count(*) FROM users; SELECT count(*) FROM audit_logs;
#   3) 对账通过 → 删演练库 dropdb sida_drill, 把行数记到 CHANGELOG/值班表。
#   4) 备份文件异地一份(对象存储/C盘各一, 生产铁律)。
