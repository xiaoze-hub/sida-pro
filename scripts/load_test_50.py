#!/usr/bin/env python3
"""Skill Gateway 压测: 50 并发 get_stock_quote。"""
import json
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "http://127.0.0.1:8000"


def _req(path, method="GET", body=None, headers=None, timeout=30):
    url = BASE + path
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode()), time.monotonic() - t0
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode()), time.monotonic() - t0
        except Exception:
            return e.code, {}, time.monotonic() - t0
    except Exception as e:
        return 0, {"err": str(e)}, time.monotonic() - t0


# 1) 领 key
st, body, _ = _req("/api/keys", "POST", {"owner_label": "loadtest", "trial": True})
data = body.get("data") if isinstance(body, dict) and "data" in body else body
KEY = data.get("api_key", "")
print(f"key={KEY[:12]}... tier={data.get('tier')} limit={data.get('daily_limit')}")
if not KEY:
    raise SystemExit("no key")

# 2) 50 并发 get_stock_quote
def one(i):
    return _req(
        "/api/skills/get_stock_quote/run",
        "POST",
        {"args": {"symbol": "600519", "market": "CN"}},
        headers={"X-API-Key": KEY},
        timeout=30,
    )

t0 = time.monotonic()
results = []
with ThreadPoolExecutor(max_workers=50) as ex:
    futs = [ex.submit(one, i) for i in range(50)]
    for f in as_completed(futs):
        results.append(f.result())
total = time.monotonic() - t0

ok = [r for r in results if r[0] == 200]
r429 = [r for r in results if r[0] == 429]
other = [r for r in results if r[0] not in (200, 429)]
lats = [r[2] for r in ok]
print(f"total={len(results)} ok={len(ok)} 429={len(r429)} other={len(other)}")
print(f"wall={total:.2f}s")
if lats:
    lats.sort()
    p50 = statistics.median(lats)
    p95 = lats[int(len(lats) * 0.95) - 1] if len(lats) >= 2 else lats[0]
    print(f"ok_p50={p50*1000:.0f}ms ok_p95={p95*1000:.0f}ms max={max(lats)*1000:.0f}ms")
print("target: 50并发 成功率100%(限流内) p95<2s")
print(f"PASS_50 = {len(r429)==0 and len(other)==0 and (not lats or p95<2.0)}")
