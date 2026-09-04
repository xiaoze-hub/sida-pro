#!/usr/bin/env python3
"""海外 K线补数通道 (P-20260904: 容器出口被 WSL 内透明代理掐断,
腾讯/东财/新浪日K直连全挂, 18:00 容器内 backfill 持续 0 行)。

由 Hermes 宿主机(海外, 直连正常)抓腾讯 fqkline, 经 SSH 管道幂等灌入生产 PG。
口径: vol(手)*100=股 (与 PG 现存行逐股核对一致), ts=D 08:00:00+08,
三源同值, ON CONFLICT DO NOTHING。无当日行的标的跳过, 不编造。

用法: python3 scripts/offsite_kline_backfill.py [YYYY-MM-DD] (缺省=今天)
环境: SSHPASS / SSH_HOST (缺省 980530 / TIANXIANG@100.91.30.35)
退出码: 0=OK(含 0 行即当日无数据), 1=执行失败。
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SSH_HOST = os.getenv("SSH_HOST", "TIANXIANG@100.91.30.35")
SSH_PASS = os.getenv("SSHPASS", "980530")
TARGET_DATE = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().isoformat()
TS = f"{TARGET_DATE} 08:00:00+08"


def ssh(remote_cmd: str, stdin: bytes | None = None) -> str:
    """经 Windows OpenSSH 到小主机。注意: Windows sshd 会重解析命令行,
    管道/引号必须包在单条 `wsl bash -c "..."` 内, 不能拆成多 argv。"""
    cmd = ["sshpass", "-p", SSH_PASS, "ssh", "-o", "StrictHostKeyChecking=no",
           "-o", "ConnectTimeout=15", SSH_HOST, remote_cmd]
    return subprocess.run(cmd, input=stdin, capture_output=True, timeout=180).stdout.decode(
        "utf-8", "ignore")


def wsl_bash(script: str) -> str:
    return 'wsl bash -c "%s"' % script.replace('"', '\\"')


def tsym(sym: str) -> str:
    if sym[0] in ("6", "9"):
        return "sh" + sym
    if sym[:2] in ("43", "83", "87", "92"):
        return "bj" + sym
    return "sz" + sym


def fetch(sym: str):
    t = tsym(sym)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={t},day,,,3,qfq"
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                       "Referer": "https://gu.qq.com/"})
            with urllib.request.urlopen(req, timeout=15) as x:
                text = x.read().decode()
            break
        except Exception as e:  # noqa: BLE001 (501 限频则退避)
            last = e
            time.sleep(5 * (attempt + 1))
    else:
        raise last if last else RuntimeError("fetch failed")
    js = text.split("=", 1)[1].strip().rstrip(";") if text.strip().startswith("kline_") else text
    rows = (json.loads(js)["data"][t].get("qfqday")
            or json.loads(js)["data"][t].get("day") or [])
    for r in reversed(rows):
        if r[0] == TARGET_DATE:
            return r
    return None


def main() -> int:
    import base64
    qb = base64.b64encode(b"SELECT DISTINCT symbol, market FROM stocks;").decode()
    inner = (f"echo {qb} | base64 -d | sudo docker exec -i panwatch-postgres "
               "psql -U sida -d sida -t -A -F,")
    raw = ssh(wsl_bash(inner))
    syms = [l.split(",") for l in raw.strip().splitlines() if "," in l]
    if not syms:
        print("FAIL 取自选名单为空")
        return 1
    vals, skipped = [], []
    for sym, mkt in syms:
        time.sleep(1.2)  # 腾讯限频 501, 礼貌间隔
        try:
            r = fetch(sym)
        except Exception as e:  # noqa: BLE001
            skipped.append((sym, str(e)[:50]))
            continue
        if not r:
            skipped.append((sym, "no-row"))
            continue
        o, c, h, low, v = float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(float(r[5]) * 100)
        for src in ("tencent", "eastmoney", "sina"):
            vals.append(f"('{TS}','{sym}','{mkt}','1d','{src}',{o},{h},{low},{c},{v},1)")
    if vals:
        sql = ("INSERT INTO klines (ts,symbol,market,period,source,open,high,low,close,volume,"
               "quality_flag) VALUES\n" + ",\n".join(vals) +
               "\nON CONFLICT (symbol, market, period, ts, source) DO NOTHING;")
        ssh(wsl_bash("sudo docker exec -i panwatch-postgres psql -U sida -d sida "
                       "-v ON_ERROR_STOP=1"),
            stdin=sql.encode())
    print(f"OK date={TARGET_DATE} symbols={len(syms)} rows={len(vals)} skipped={len(skipped)}")
    for s in skipped[:10]:
        print("  skip:", s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
