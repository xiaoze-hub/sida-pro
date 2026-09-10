#!/bin/bash
# 通达信龙虎榜缓存 → 容器可读目录 (2026-09-10, 方案: 老板点开客户端"龙虎榜"页 → 本地缓存 → 本脚本同步)
#
# 背景: 龙虎榜数据在通达信客户端属"云数据"(TQ 接口取不到, 需数据权限), 但客户端
# 打开页面后会把数据落到 C:\new_tdx64\T0002\cloud_cache\... 本脚本在 WSL 里把
# 这份缓存拷进 panwatch 容器的数据卷(/app/data/tdx_lhb), 应用侧只读解析。
#
# 用法(生产=本机 WSL, 需 root 写 docker volume):
#   wsl -u root -e bash /mnt/c/Users/tianxiang/sida-work/scripts/sync_tdx_lhb_cache.sh
# 建议由计划任务每 5 分钟跑一次(见 README 说明; 仅当客户端缓存有更新时才真正拷贝)。
set -uo pipefail

SRC="${TDX_CACHE_SRC:-/mnt/c/new_tdx64/T0002/cloud_cache}"
DST="${TDX_CACHE_DST:-/var/lib/docker/volumes/panwatch-data/_data/tdx_lhb}"
LOG="${TDX_SYNC_LOG:-/tmp/tdx_lhb_sync.log}"

mkdir -p "$DST/list" "$DST/lhbfx" 2>/dev/null || true

changed=0
board="$SRC/list/func_lhbfx101_1.jsn"
if [ -f "$board" ]; then
  if ! cmp -s "$board" "$DST/list/func_lhbfx101_1.jsn" 2>/dev/null; then
    cp -f "$board" "$DST/list/func_lhbfx101_1.jsn" && changed=1
  fi
else
  echo "[$(date '+%F %T')] WARN 客户端榜单缓存不存在: $board (客户端未打开过龙虎榜页?)" >> "$LOG"
fi

if [ -d "$SRC/lhbfx" ]; then
  for f in "$SRC"/lhbfx/*.jsn; do
    [ -e "$f" ] || continue
    b="$(basename "$f")"
    if ! cmp -s "$f" "$DST/lhbfx/$b" 2>/dev/null; then
      cp -f "$f" "$DST/lhbfx/$b" && changed=1
    fi
  done
fi

seats=$(ls "$DST/lhbfx" 2>/dev/null | wc -l | tr -d ' ')
echo "[$(date '+%F %T')] ok changed=$changed board=$([ -f "$DST/list/func_lhbfx101_1.jsn" ] && echo yes || echo no) seats=$seats" >> "$LOG"
exit 0
