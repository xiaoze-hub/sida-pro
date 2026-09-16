#!/bin/bash
set -euo pipefail
# KI-057 历史振幅回填(dry-run 先看规模)
docker cp /mnt/c/Users/tianxiang/sida-work/scripts/backfill_amplitude_prev_close.py panwatch:/tmp/bf_amp.py
echo "=== dry-run ==="
docker exec -u app panwatch python /tmp/bf_amp.py --dry-run --limit 5000
echo "=== apply (full) ==="
docker exec -u app panwatch python /tmp/bf_amp.py
echo BACKFILL_DONE
