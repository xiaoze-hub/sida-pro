"""通达信本地标准网关（P2）。

    POST /                    JSON-RPC 直通（和 17709 同形状，SIDA 现有 vendor 可零改动切过来）
    POST /rpc/{method}        REST 便捷口（只传业务参数，id/type/table_list 由契约层补齐）
    GET  /api/quote/{symbol}  单只快照
    POST /api/quotes          批量快照
    GET  /health              三项自检（上游可达 / 延迟 / 熔断）
    GET  /metrics             方法级 ok/err/缓存/延迟

红线：下单/撤单/资金/exec_to_tdx 一律 403，任何配置都不放开。
"""
from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from .contract import DENY, ContractError, load_schema
from .upstream import Upstream, UpstreamBusy, UpstreamUnavailable

SCHEMA_PATH = os.getenv("TDX_SCHEMA", "/app/contract/http_schema.json")
UPSTREAM = os.getenv("TDX_UPSTREAM", "http://172.27.16.1:17709/")
CONCURRENCY = int(os.getenv("TDX_CONCURRENCY", "1"))     # 默认串行：客户端经不起并发
QUEUE_LIMIT = int(os.getenv("TDX_QUEUE_LIMIT", "64"))

up: Upstream | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global up
    up = Upstream(UPSTREAM, load_schema(SCHEMA_PATH), concurrency=CONCURRENCY, queue_limit=QUEUE_LIMIT)
    await up.start()
    try:
        yield
    finally:
        await up.close()


app = FastAPI(title="TDX Local Gateway", version="1.0.0", lifespan=lifespan)


class RpcBody(BaseModel):
    id: int | str | None = None
    method: str | None = None
    params: dict | None = None


def _guard(method: str) -> None:
    if method in DENY:
        raise HTTPException(403, f"{method} 属于交易/资金/客户端执行类，网关不开放")


async def _call(method: str, params: dict) -> dict:
    assert up is not None
    _guard(method)
    try:
        res = await up.call(method, params or {})
    except UpstreamBusy as e:
        raise HTTPException(429, str(e))
    except UpstreamUnavailable as e:
        raise HTTPException(503, str(e))
    except ContractError as e:
        raise HTTPException(400, str(e))
    return res if isinstance(res, dict) else {"Value": res}


@app.post("/")
async def jsonrpc(body: RpcBody):
    """与 17709 同形状：{"id":1,"method":"...","params":{...}} -> {"id":1,"result":{...}}"""
    if not body.method:
        return JSONResponse({"error": {"code": -32600, "message": "缺少 method"}}, status_code=400)
    res = await _call(body.method, body.params or {})
    return {"id": body.id or 1, "result": res}


@app.post("/rpc/{method}")
async def rpc(method: str, params: dict | None = None):
    return await _call(method, params or {})


@app.get("/api/quote/{symbol}")
async def quote(symbol: str):
    res = await _call("get_market_snapshot", {"stock_code": symbol})
    if str(res.get("ErrorId", "0")) != "0":
        raise HTTPException(502, res.get("Error") or f"ErrorId={res.get('ErrorId')}")
    return res


@app.post("/api/quotes")
async def quotes(symbols: list[str]):
    out: dict[str, dict] = {}
    for s in symbols[:50]:                       # 串行，不做并发
        try:
            res = await _call("get_market_snapshot", {"stock_code": s})
            out[s] = res
        except HTTPException as e:
            out[s] = {"error": str(e.detail)}
    return out


@app.get("/health")
async def health():
    assert up is not None
    t0 = time.perf_counter()
    probe: dict = {"ok": False, "ms": None, "err": None}
    try:
        res = await up.call("get_market_snapshot", {"stock_code": "600111.SH"})
        probe["ms"] = round((time.perf_counter() - t0) * 1000)
        probe["ok"] = str(res.get("ErrorId", "0")) == "0"
        if not probe["ok"]:
            probe["err"] = res.get("Error")
    except Exception as e:
        probe["err"] = f"{type(e).__name__}: {e}"

    status = "ok" if probe["ok"] else ("degraded" if up.circuit_open else "down")
    code = 200 if probe["ok"] else 503
    return JSONResponse({"status": status, "upstream": up.base_url, "probe": probe,
                         "circuit_open": up.circuit_open,
                         "concurrency": CONCURRENCY}, status_code=code)


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    assert up is not None
    snap = up.snapshot_metrics()
    lines = ["# HELP tdx_method_ok_total 成功调用数", "# TYPE tdx_method_ok_total counter"]
    for m, v in snap["methods"].items():
        lines.append(f'tdx_method_ok_total{{method="{m}"}} {v["ok"]}')
        lines.append(f'tdx_method_err_total{{method="{m}"}} {v["err"]}')
        lines.append(f'tdx_method_cache_hit_total{{method="{m}"}} {v["cache_hit"]}')
        if v["avg_ms"] is not None:
            lines.append(f'tdx_method_avg_ms{{method="{m}"}} {v["avg_ms"]}')
    lines.append(f"tdx_circuit_open {1 if snap['circuit_open'] else 0}")
    lines.append(f"tdx_inflight {snap['inflight']}")
    return "\n".join(lines) + "\n"
