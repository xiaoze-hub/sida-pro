import json
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from src.core.demon_factors import recompute_factors
from src.core.lhb_backfill import backfill_history

stats = backfill_history(days=365)
print("LHB_BACKFILL:", json.dumps(stats, ensure_ascii=False))
rec = recompute_factors()
print("FACTORS:", json.dumps(rec, ensure_ascii=False))
