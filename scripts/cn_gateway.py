#!/usr/bin/env python3
"""
# CN 数据网关 - 国内服务器专用(2026-08-10 落地版, 115.190.177.213:8100)

解决香港节点访问国内数据源受限问题:
- 东财 push2delay 实时资金流(今日主力净流入, 盘中实时) — 香港 502/断连
- 东财行情快照 — 香港 502
- 关键: 主站 push2 的 ulist.np 只要带资金流字段(f62/f184/f66/f72)就被风控断连,
  必须用 push2delay 域名(延迟行情, 资金字段稳定)

香港主服务通过 http://<本机>:8100/cn/* 取数, 失败自动回退旧源。
"""
import time
import logging
import json

from fastapi import FastAPI
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cn-gateway")

app = FastAPI(title="CN Data Gateway", version="0.1.0")

# 东财字段: f62=今日主力净流入(元) f184=主力净占比(×100)
# f66=超大单净流入 f72=大单净流入 f78=中单净流入 f84=小单净流入
FLOW_FIELDS2 = "f2,f3,f12,f14,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87"
# 主站 push2 资金流字段会被风控断连(2026-08-10 实测), push2delay(延迟行情)稳定
_FLOW_BASE = "https://push2delay.eastmoney.com/api/qt/ulist.np/get"


def _http_get(url: str, referer: str = "https://data.eastmoney.com/", timeout: float = 8) -> dict:
    """GET 请求带 Referer(东财要求), 返回 JSON。requests 重试3次(东财连接不稳)。"""
    import requests
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
    }
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
        time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"东财请求失败(3次): {last_err}")


def _secid(code: str) -> str:
    """A股代码 → 东财 secid(沪 1. 深 0.)"""
    code = code.strip()
    if code.startswith(("6", "5", "9")):
        return f"1.{code}"
    return f"0.{code}"


@app.get("/health")
def health():
    return {"status": "ok", "ts": int(time.time())}


@app.get("/cn/stock-flow/{code}")
def stock_flow(code: str):
    """个股今日实时资金流(东财 push2delay, 盘中实时)。

    f62=主力净流入 f66=超大单 f72=大单 f78=中单 f84=小单(元)
    f184=主力净占比(×100), f69/f75/f81/f87=各档占比
    """
    try:
        secid = _secid(code)
        url = f"{_FLOW_BASE}?secids={secid}&fields={FLOW_FIELDS2}"
        d = _http_get(url)
        diff = (d.get("data") or {}).get("diff") or []
        if not diff:
            return JSONResponse({"error": f"无数据 {code}"}, status_code=404)
        it = diff[0]
        return {
            "code": code,
            "name": it.get("f14"),
            "price": it.get("f2"),          # ×100
            "change_pct": it.get("f3"),     # ×100
            "main_net_inflow": it.get("f62"),       # 主力净流入(元) 今日实时
            "main_net_pct": it.get("f184"),         # 主力净占比(×100)
            "super_net_inflow": it.get("f66"),      # 超大单净流入
            "super_net_pct": it.get("f69"),
            "big_net_inflow": it.get("f72"),        # 大单净流入
            "big_net_pct": it.get("f75"),
            "mid_net_inflow": it.get("f78"),        # 中单净流入
            "small_net_inflow": it.get("f84"),      # 小单净流入
            "source": "eastmoney_push2delay",
            "ts": int(time.time()),
        }
    except Exception as e:
        log.warning(f"stock_flow {code} 失败: {e}")
        return JSONResponse({"error": str(e)[:100]}, status_code=502)


@app.get("/cn/stock-flow-batch")
def stock_flow_batch(codes: str):
    """批量个股实时资金流。codes=600519,000001"""
    out = {}
    for code in codes.replace(" ", "").split(","):
        if not code:
            continue
        secid = _secid(code)
        try:
            url = f"{_FLOW_BASE}?secids={secid}&fields={FLOW_FIELDS2}"
            d = _http_get(url)
            diff = (d.get("data") or {}).get("diff") or []
            if diff:
                it = diff[0]
                out[code] = {
                    "name": it.get("f14"),
                    "main_net_inflow": it.get("f62"),
                    "main_net_pct": it.get("f184"),
                    "super_net_inflow": it.get("f66"),
                    "big_net_inflow": it.get("f72"),
                }
        except Exception as e:
            out[code] = {"error": str(e)[:80]}
    return {"codes": out, "ts": int(time.time())}

@app.get("/cn/quote/{code}")
def quote(code: str):
    """个股实时行情(东财, 含今日实时)。"""
    try:
        secid = _secid(code)
        url = f"https://push2.eastmoney.com/api/qt/ulist.np/get?secids={secid}&fields=f2,f3,f4,f5,f6,f12,f14,f15,f16,f17,f18"
        d = _http_get(url)
        diff = (d.get("data") or {}).get("diff") or []
        if not diff:
            return JSONResponse({"error": f"无数据 {code}"}, status_code=404)
        it = diff[0]
        return {
            "code": code, "name": it.get("f14"),
            "price": it.get("f2"), "change_pct": it.get("f3"), "change_amount": it.get("f4"),
            "volume": it.get("f5"), "amount": it.get("f6"),
            "high": it.get("f15"), "low": it.get("f16"), "open": it.get("f17"), "prev_close": it.get("f18"),
            "source": "eastmoney_push2", "ts": int(time.time()),
        }
    except Exception as e:
        return JSONResponse({"error": str(e)[:100]}, status_code=502)


# ── 扩展端点(2026-08-10 生产验证; 见 ashare-data-tooling/references/cn-gateway-endpoints.md) ──
import requests as _rq

_HDRS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
         "Referer": "https://quote.eastmoney.com/"}


@app.get("/cn/hot-stocks")
def hot_stocks(mode: str = "turnover", limit: int = 20):
    """热门股票榜单(东财 clist push2delay, 国内直连; 海外 push2 502)。"""
    try:
        fid = "f6" if mode == "turnover" else "f3"   # f6=成交额榜 f3=涨幅榜
        url = (
            "https://push2delay.eastmoney.com/api/qt/clist/get"
            f"?pn=1&pz={limit}&po=1&np=1&fltt=2&invt=2&fid={fid}"
            "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
            "&fields=f12,f14,f2,f3,f6,f5"
        )
        d = _http_get(url, referer="https://quote.eastmoney.com/")  # 3 次退避重试
        diff = (d.get("data") or {}).get("diff") or []
        return {"items": [{"symbol": it.get("f12"), "name": it.get("f14"),
                           "price": it.get("f2"), "change_pct": it.get("f3"),
                           "turnover": it.get("f6"), "volume": it.get("f5")} for it in diff],
                "source": "eastmoney_clist_cn", "ts": int(time.time())}
    except Exception as e:
        log.warning(f"hot_stocks {mode} 失败: {e}")
        return JSONResponse({"error": str(e)[:100]}, status_code=502)


@app.get("/cn/hot-boards")
def hot_boards(limit: int = 12):
    """热门板块榜单。"""
    try:
        url = ("https://push2delay.eastmoney.com/api/qt/clist/get"
               f"?pn=1&pz={limit}&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:2"
               "&fields=f12,f14,f2,f3,f6,f5")
        d = _http_get(url, referer="https://quote.eastmoney.com/")  # 3 次退避重试
        diff = (d.get("data") or {}).get("diff") or []
        return {"items": [{"code": it.get("f12"), "name": it.get("f14"),
                           "change_pct": it.get("f3"), "turnover": it.get("f6")} for it in diff],
                "source": "eastmoney_clist_cn", "ts": int(time.time())}
    except Exception as e:
        log.warning(f"hot_boards 失败: {e}")
        return JSONResponse({"error": str(e)[:100]}, status_code=502)


OV = {"ts": 0.0, "data": None}
OV_TTL_S = 60


def _datacenter_overview() -> dict:
    """2026-09-28 兜底上游: push2 的 ulist.np 路径已被东财边缘层按 URL 拒掉(零字节关闭),
    实测 datacenter-web 的 RPT_MARKET_CAPITALFLOW 报表可用且同口径(按单金额四档归类)。

    字段: MAIN_INFLOW/MAIN_OUTFLOW(主力=超大单+大单), SUPERDEAL_NET, BIGDEAL_NET,
          MIDDEAL_NET, SMALLDEAL_NET, INNUM/OUTNUM(涨/跌家数), CHANGERATE(涨跌幅)。
    单位: 万元 -> /1e4 得亿。point(指数点位)本报表不提供 -> 置 None, 前端显示 "--",
          但**资金字段是真实值**, 不用降级值冒充。
    """
    u = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
         "?reportName=RPT_MARKET_CAPITALFLOW&columns=ALL&pageSize=500&pageNumber=1"
         "&sortColumns=TRADE_DATE&sortTypes=-1&source=WEB&client=WEB")
    d = _http_get(u, referer="https://data.eastmoney.com/")
    rows = ((d.get("result") or {}).get("data")) or []
    if not rows:
        raise RuntimeError("datacenter 报表无数据")
    latest = str(rows[0].get("TRADE_DATE"))[:10]
    rows = [r for r in rows
            if str(r.get("TRADE_DATE"))[:10] == latest and str(r.get("BONDTYPE")) == "AB股"]
    if not rows:
        raise RuntimeError("datacenter 报表无 AB股 行")

    def pick(prefix: str):
        for r in rows:
            if str(r.get("PLATE", "")).startswith(prefix):
                return r
        return None

    def flow_yi(r) -> float:
        if not r:
            return 0.0
        return round(((r.get("MAIN_INFLOW") or 0) - (r.get("MAIN_OUTFLOW") or 0)) / 1e4, 1)

    sh, sz, cy = pick("上证"), pick("深证"), (pick("创业板") or pick("创业"))
    if sh is None or sz is None:
        raise RuntimeError("缺少上证/深证行")
    return {
        "sh": {"name": sh.get("PLATE"), "point": None, "change_pct": sh.get("CHANGERATE"),
               "main_flow": flow_yi(sh)},
        "sz": {"name": sz.get("PLATE"), "point": None, "change_pct": sz.get("CHANGERATE"),
               "main_flow": flow_yi(sz)},
        "cyb": {"name": (cy or {}).get("PLATE"), "point": None,
                "change_pct": (cy or {}).get("CHANGERATE"), "main_flow": flow_yi(cy)},
        "total_main_flow": round(flow_yi(sh) + flow_yi(sz), 1),
        "up_count": (sh.get("INNUM") or 0) + (sz.get("INNUM") or 0),
        "down_count": (sh.get("OUTNUM") or 0) + (sz.get("OUTNUM") or 0),
        "flat_count": 0,
        "source": "eastmoney_datacenter_cn", "ts": int(time.time()),
    }



@app.get("/cn/market-overview")
def market_overview():
    """两市主力净流入 + 成交额 + 涨跌家数。

    口径对齐同花顺APP(见 docs/_frozen/caliber_matrix.md): eastmoney4 四档归类汇总。
    2026-09-28: push2 的 ulist.np 路径被东财边缘层拒掉后, 增加 datacenter 报表兜底
    (同口径, 见 _datacenter_overview); 两条都失败才返回 502, 由主服务显式降级。
    """
    if OV["data"] and time.time() - OV["ts"] < OV_TTL_S:
        return OV["data"]
    err = None
    try:
        u = ("https://push2delay.eastmoney.com/api/qt/ulist.np/get"
             "?secids=1.000001,0.399001,0.399006"
             "&fields=f2,f3,f6,f12,f14,f62,f184,f104,f105,f106")
        d = _http_get(u, referer="https://quote.eastmoney.com/")  # 3 次退避重试
        diff = (d.get("data") or {}).get("diff") or []
        sh = next((x for x in diff if x.get("f12") == "000001"), {})
        sz = next((x for x in diff if x.get("f12") == "399001"), {})
        cy = next((x for x in diff if x.get("f12") == "399006"), {})
        if not sh or sh.get("f62") is None:
            raise RuntimeError("push2 无资金字段")
        sh_flow, sz_flow = (sh.get("f62") or 0) / 1e8, (sz.get("f62") or 0) / 1e8
        out = {
            "sh": {"name": sh.get("f14"), "point": sh.get("f2"), "change_pct": sh.get("f3"), "main_flow": round(sh_flow, 1)},
            "sz": {"name": sz.get("f14"), "point": sz.get("f2"), "change_pct": sz.get("f3"), "main_flow": round(sz_flow, 1)},
            "cyb": {"name": cy.get("f14"), "point": cy.get("f2"), "change_pct": cy.get("f3"), "main_flow": round((cy.get("f62") or 0) / 1e8, 1)},
            "total_main_flow": round(sh_flow + sz_flow, 1),
            "total_amount": round(((sh.get("f6") or 0) + (sz.get("f6") or 0)) / 1e8, 0),
            "up_count": (sh.get("f104") or 0) + (sz.get("f104") or 0),
            "down_count": (sh.get("f105") or 0) + (sz.get("f105") or 0),
            "flat_count": (sh.get("f106") or 0) + (sz.get("f106") or 0),
            "source": "eastmoney_push2delay_cn", "ts": int(time.time()),
        }
        OV["ts"], OV["data"] = time.time(), out
        return out
    except Exception as e:  # noqa: BLE001
        err = e
        log.warning(f"market_overview push2 失败, 转 datacenter 兜底: {e}")
    try:
        out = _datacenter_overview()
        out["fallback_from"] = f"push2: {str(err)[:60]}"
        OV["ts"], OV["data"] = time.time(), out
        return out
    except Exception as e2:  # noqa: BLE001
        log.warning(f"market_overview datacenter 兜底也失败: {e2}")
        return JSONResponse({"error": f"push2={str(err)[:50]}; datacenter={str(e2)[:50]}"}, status_code=502)



if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8100)
