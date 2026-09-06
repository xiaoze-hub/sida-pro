import json
import sys

sys.path.insert(0, "/app")

from src.core.demon_factors import backfill_direct_tq, load_factor_pool, recompute_factors, _stock_names

names = _stock_names()
print(f"stocks: {len(names)}", flush=True)
stats = backfill_direct_tq(list(names.keys()), names)
print("BACKFILL:", json.dumps(stats, ensure_ascii=False), flush=True)
rec = recompute_factors(None)
print("FACTORS:", json.dumps(rec, ensure_ascii=False), flush=True)
pool = load_factor_pool(topn=10)
print("TOP10:", json.dumps(pool, ensure_ascii=False, default=str), flush=True)
