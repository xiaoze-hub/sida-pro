import json
import py_compile
import sys

sys.path.insert(0, "/app")

print("=== 1. 热补丁文件编译检查 ===", flush=True)
files = [
    "/app/src/core/ambush_score.py",
    "/app/src/core/demon_factors.py",
    "/app/src/core/demon_score.py",
    "/app/src/core/seal_sampler.py",
    "/app/src/core/seal_quality.py",
    "/app/src/core/signal_review.py",
    "/app/src/core/l2_event_stream.py",
    "/app/src/core/mood_cycle.py",
    "/app/src/agents/premarket_outlook.py",
    "/app/server.py",
    "/app/packages/marketdata/src/marketdata/vendors/tq.py",
]
for f in files:
    try:
        py_compile.compile(f, doraise=True)
        print(f"  OK {f}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  COMPILE_FAIL {f}: {e}", flush=True)

print("\n=== 2. DB 状态核查 ===", flush=True)
from sqlalchemy import text

from src.web.database import SessionLocal

db = SessionLocal()
try:
    cfg = db.execute(
        text("SELECT name, enabled, schedule FROM agent_configs WHERE name = 'premarket_outlook'")
    ).first()
    print(f"  premarket_outlook 配置: {tuple(cfg) if cfg else 'MISSING(需创建)'}", flush=True)
    mp = db.execute(text("SELECT date, phase FROM market_phase_daily ORDER BY date DESC LIMIT 1")).first()
    print(f"  market_phase_daily 最新: {tuple(mp) if mp else 'EMPTY(周一情绪维按中性5降级)'}", flush=True)
    ch = db.execute(
        text("SELECT COUNT(*) FROM notify_channels WHERE enabled = true AND user_id IS NULL")
    ).scalar()
    print(f"  全局通知渠道: {ch} 个(0=简报只进站内不发微信)", flush=True)
    for t in ("demon_factors", "limit_up_events", "seal_quality_samples", "signal_snapshots"):
        n = db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
        print(f"  {t}: {n} 行", flush=True)
    d = db.execute(text("SELECT MAX(trade_date) FROM limit_up_events")).scalar()
    print(f"  limit_up_events 最新交易日: {d}", flush=True)
    n = db.execute(text("SELECT COUNT(DISTINCT symbol) FROM stocks")).scalar()
    print(f"  自选股 distinct symbols: {n}", flush=True)
finally:
    db.close()

print("\n=== 3. 磁盘 ===", flush=True)
import subprocess

disk = subprocess.run(["df", "-h", "/app/data"], capture_output=True, text=True)
print(disk.stdout, flush=True)
print("=== 审查完成 ===", flush=True)
