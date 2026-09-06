import json
import sys

sys.path.insert(0, "/app")

from src.core.limit_up_backfill import backfill_all

limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
r = backfill_all(max_stocks=limit)
print(json.dumps(r, ensure_ascii=False), flush=True)
