import json
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/packages/marketdata/src")

from marketdata.symbol import Symbol as _Sym
from marketdata.vendors.tq import to_tq_code as _tqc, tq_rpc

from src.core.limit_rules import limit_ratio

# 1) 周五(20260904)封板收 ∩ 妖股因子 Top50 → FCAmo vs 涨停池封单额对照
day = "20260904"
from sqlalchemy import text

from src.web.database import SessionLocal

db = SessionLocal()
rows = db.execute(
    text(
        "SELECT e.symbol, e.name, f.total FROM limit_up_events e"
        " JOIN demon_factors f ON f.symbol = e.symbol"
        " WHERE e.trade_date = :day AND e.is_sealed_close = 1"
        " ORDER BY f.total DESC LIMIT 5"
    ),
    {"day": day},
).fetchall()
db.close()

pool = {}
try:
    from src.collectors.market_sentiment_collector import MarketSentimentCollector

    for p in MarketSentimentCollector().get_limit_up_pool(day) or []:
        pool[str(p.get("code"))] = p
except Exception as e:  # noqa: BLE001
    print("POOL_FAIL:", repr(e), flush=True)

print("=== FCAmo 对照(周五涨停股) ===", flush=True)
for r in rows:
    sym = r[0]
    tqc = None
    try:
        tqc = _tqc(_Sym.parse(sym, "CN"))
        raw = tq_rpc("get_more_info", {"stock_code": tqc})
        fc_amo = raw.get("FCAmo")
        fc_b = raw.get("FCb")
    except Exception as e:  # noqa: BLE001
        print(f"{sym} {r[1]} total={r[2]}: TQ失败 {e!r}", flush=True)
        continue
    p = pool.get(sym)
    amount = p.get("amount") if p else None
    ratio = round(float(fc_amo) / float(amount), 3) if (fc_amo and amount) else None
    print(
        f"{sym} {r[1]} total={r[2]} | FCAmo={fc_amo} FCb={fc_b} | 池封单额={amount} | 比值={ratio}"
        f" | 板={limit_ratio(sym)}",
        flush=True,
    )

# 2) 妖股领先效应回测(真实事件+真实K线)
print("\n=== 妖股领先效应回测 ===", flush=True)
sys.argv = ["backtest", "--max-events", "40", "--demon-n", "10"]
import subprocess

out = subprocess.run(
    ["python", "/app/scripts/backtest_demon_leading.py", "--max-events", "40", "--demon-n", "10"],
    capture_output=True, text=True, timeout=1500,
)
print(out.stdout[-3000:], flush=True)
if out.returncode != 0:
    print("BACKTEST_ERR:", out.stderr[-1500:], flush=True)
