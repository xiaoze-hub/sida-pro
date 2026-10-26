"""通达信 TQ 个股「异动资金档案」：大宗交易 / 增减持 / 北向持股 / 质押 / 财务指标。

为什么单开一层（2026-10-20 用户裁决：逆向接口优先、弱源备用）：
这五类数据在东财要分别拉四个 datacenter 接口、还可能被限流；TQ 的 `get_gpjy_value`
一次就能拿全，且零配额、本地 RPC。

## 单位与语义（**全部用实测交叉验证过，不是照文档抄**）

| 表 | 内容 | 单位（实测标定） | 验证方法 |
|---|---|---|---|
| GP04 | 大宗交易 | `[成交均价(元), 成交额(万元)]` | 600519 20261015 = `[1312.97, 407.02]`，均价与该股当日 1324.97 同量级 ✓ |
| GP05 | 股东增减持 | `[成交均价(元), 变动股数(股)]` | 600519 = `[1443.14, 1274234]`，127 万股对茅台量级合理 ✓ |
| GP06 | 陆股通持股量 | `[持股数量(股)]` | 600519 = 55,263,680 股 ≈ 5526 万股 ✓ |
| GP07 | 陆股通成交净额 | `[净额(万元)]` | 末值止于 20240816（北向停披露），如实标注 |
| GP19 | 每周质押数量 | **万股**（⚠️ 官方文档写\"万元\"是错的） | 002361 = 6354 万股 / 总股本 95103.5 万股 = **6.68%**，与 GP20 的 6.68% 精确吻合 ✓ |
| GP20 | 每周质押比例 | `[比例(%)]` | 茅台 0.06% / 002361 6.68%，量级合理 ✓ |
| FN1 / FN3 | 每股收益 / 每股未分配利润 | 元 | 600519 各 10 期，配 `announce_time` 日期数组 ✓ |

## 未标定的编号（**明确不接**，别猜）
`GP21 / GP25 / GP27 / GP30` 实测都有数据，但官方文档只定义到 GP20，本仓
`docs/_archive/20261026_design/涨停池_tdx源_设计_20260923.md` 只覆盖 GP14/22/24/38-40（涨停族）。
这几张表与成交量/成交额/换手率都做过相关性对比，**没有一组能对上量纲**
（例：002361 GP25 与当日成交量比值在 14,876~49,207 之间乱跳），
=> 语义未确认前一律不接，避免"看着像就编字段名"。
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

# 17709 是单线程薄 RPC：连打会被客户端 reset（技能实测"每次约 2s 后 reset"）。
# 这里串行化 + 最小间隔 + 失败退避重试，避免"五类数据一起拉"时把连接打崩。
_PACE_LOCK = threading.Lock()
_LAST_CALL = [0.0]
_MIN_INTERVAL = 0.25
_RETRY = 3

SOURCE = "tdx_tq_gp"
_CACHE: dict[tuple, tuple[float, dict]] = {}
_CACHE_TTL = 600.0   # 10min：这类数据是日频/周频，不需要更实时


def _tq_code(symbol: str) -> str | None:
    """裸码 → 带后缀（TQ 的 *_jy_value 必须带后缀，裸码报 codestr error）。"""
    s = str(symbol or "").strip().upper()
    if not s:
        return None
    if "." in s:
        return s
    if s.startswith(("6", "9", "5")):
        return f"{s}.SH"
    if s.startswith(("0", "3", "1", "2")):
        return f"{s}.SZ"
    if s.startswith(("4", "8")):
        return f"{s}.BJ"
    return None


def _gp_rows(symbol: str, table: str, start: str, end: str) -> list[dict] | None:
    """取一张 GP 表；返回 [{"date","v":[float,...]}]，失败/空返回 None（区分"没查到"与"没有"）。"""
    code = _tq_code(symbol)
    if not code:
        return None
    resp = None
    last_err: Exception | None = None
    for attempt in range(_RETRY):
        try:
            from marketdata.vendors.tq import tq_rpc

            with _PACE_LOCK:
                gap = time.time() - _LAST_CALL[0]
                if gap < _MIN_INTERVAL:
                    time.sleep(_MIN_INTERVAL - gap)
                resp = tq_rpc(
                    "get_gpjy_value",
                    {"table_list": [table], "code": code,
                     "start_time": start, "end_time": end},
                )
                _LAST_CALL[0] = time.time()
            last_err = None
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(0.4 * (attempt + 1))   # 退避，别把单线程客户端打崩
    if last_err is not None:
        logger.warning("[tdx-corporate] %s %s 取数失败(重试 %d 次): %s",
                       code, table, _RETRY, last_err)
        return None
    # ⚠️ 网关对"该窗口没有这类事件"返回的是 **None**（不是空字典，也不报错）。
    # 把它当失败会让 002361 这种"确实没大宗"的票显示"取数失败" —— 把"没有"说成"查不到"。
    if resp is None:
        return []
    if not isinstance(resp, dict):
        return None
    rows = resp.get(table)
    if rows is None and isinstance(resp.get("Value"), dict):
        rows = resp["Value"].get(table)     # 少数版本把表塞进 Value
    out: list[dict] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        vals = r.get("Value") or []
        try:
            nums = [float(x) for x in vals]
        except (TypeError, ValueError):
            continue
        out.append({"date": str(r.get("Date") or r.get("date") or ""), "v": nums})
    return out


def _window(days: int) -> tuple[str, str]:
    import datetime as _dt

    end = _dt.date.today()
    start = end - _dt.timedelta(days=max(1, int(days)))
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _cached(key: tuple, fn):
    hit = _CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    val = fn()
    if val is not None:
        _CACHE[key] = (now, val)
    return val


def block_trades(symbol: str, days: int = 365) -> dict:
    """大宗交易：单价(元) + 成交额(元)。TQ 为空 = 该股期间无大宗（不是取不到）。"""
    start, end = _window(days)

    def _load():
        return _gp_rows(symbol, "GP04", start, end)

    rows = _cached(("GP04", _tq_code(symbol), start, end), _load)
    if rows is None:
        return {"available": False, "items": [], "source": SOURCE, "note": "通达信取数失败"}
    items = [
        {
            "date": r["date"],
            "price": r["v"][0] if len(r["v"]) > 0 else None,
            "amount": round(r["v"][1] * 1e4, 2) if len(r["v"]) > 1 else None,  # 万元 → 元
        }
        for r in rows
    ]
    total = sum(i["amount"] or 0 for i in items) or None
    return {
        "available": True,
        "items": items,
        "count": len(items),
        "amount_total": total,
        "source": SOURCE,
        "caliber": "GP04 大宗交易(均价/成交额)，单位已换算为元",
        "note": None if items else "该股在近一年内无大宗交易记录（TQ 口径，窗口可放宽）",
    }


def holder_changes(symbol: str, days: int = 730) -> dict:
    """股东增减持：均价(元) + 变动股数(股，正=增持/负=减持按 TQ 原值)。"""
    start, end = _window(days)

    def _load():
        return _gp_rows(symbol, "GP05", start, end)

    rows = _cached(("GP05", _tq_code(symbol), start, end), _load)
    if rows is None:
        return {"available": False, "items": [], "source": SOURCE, "note": "通达信取数失败"}
    items = [
        {
            "date": r["date"],
            "price": r["v"][0] if len(r["v"]) > 0 else None,
            "shares": r["v"][1] if len(r["v"]) > 1 else None,
        }
        for r in rows
    ]
    return {
        "available": True,
        "items": items,
        "count": len(items),
        "source": SOURCE,
        "caliber": "GP05 股东增减持(均价/变动股数)",
        "note": None if items else "该股在近两年内无增减持记录（TQ 口径，窗口可放宽）",
    }


def northbound_holding(symbol: str, days: int = 180) -> dict:
    """陆股通**持股量**日序列（北向净买入已停披露，持股量存量仍在）。"""
    start, end = _window(days)

    def _load():
        return _gp_rows(symbol, "GP06", start, end)

    rows = _cached(("GP06", _tq_code(symbol), start, end), _load)
    if rows is None:
        return {"available": False, "items": [], "source": SOURCE, "note": "通达信取数失败"}
    items = [
        {"date": r["date"], "shares": r["v"][0] if r["v"] else None}
        for r in rows
    ]
    latest = items[-1] if items else None
    prev = items[-2] if len(items) > 1 else None
    delta = None
    if latest and prev and latest["shares"] is not None and prev["shares"] is not None:
        delta = latest["shares"] - prev["shares"]
    return {
        "available": True,
        "items": items[-60:],
        "count": len(items),
        "latest": latest,
        "delta": delta,
        "source": SOURCE,
        "caliber": "GP06 陆股通持股数量(股)；净买入已停披露，这里只在有存量时给持股量与变化",
        "note": None if items else "该股在所选区间内无陆股通持股记录（TQ 口径）",
    }


def pledge(symbol: str, days: int = 730) -> dict:
    """股权质押：数量(**万股**) + 比例(%)。

    ⚠️ 官方文档把 GP19 写成"万元"是错的：002361 实测 6354 万股 / 总股本 95103.5 万股 = 6.68%，
    与 GP20 的 6.68% 精确吻合 ⇒ 单位是**万股**。
    """
    start, end = _window(days)

    def _load_qty():
        return _gp_rows(symbol, "GP19", start, end)

    def _load_ratio():
        return _gp_rows(symbol, "GP20", start, end)

    qty = _cached(("GP19", _tq_code(symbol), start, end), _load_qty)
    ratio = _cached(("GP20", _tq_code(symbol), start, end), _load_ratio)
    if qty is None and ratio is None:
        return {"available": False, "items": [], "source": SOURCE, "note": "通达信取数失败"}
    by_date: dict[str, dict] = {}
    for r in qty or []:
        by_date.setdefault(r["date"], {})["shares_wan"] = r["v"][0] if r["v"] else None
    for r in ratio or []:
        by_date.setdefault(r["date"], {})["ratio_pct"] = r["v"][0] if r["v"] else None
    items = [
        {"date": d, **{k: v for k, v in vals.items() if v is not None}}
        for d, vals in sorted(by_date.items())
    ]
    latest = items[-1] if items else None
    return {
        "available": True,
        "items": items[-26:],       # 约半年周频
        "count": len(items),
        "latest": latest,
        "source": SOURCE,
        "caliber": "GP19 质押数量(万股) + GP20 质押比例(%)；单位经 002361 交叉验证",
        "note": None if items else "该股在近两年内无质押记录（TQ 口径，窗口可放宽）",
    }


def financials(symbol: str, days: int = 800) -> dict:
    """财务指标（TQ 专业财务）：FN1 每股收益 / FN3 每股未分配利润，配公告日。"""
    code = _tq_code(symbol)
    start, end = _window(days)

    def _load():
        from marketdata.vendors.tq import tq_rpc

        try:
            return tq_rpc(
                "get_financial_data",
                {
                    "table_list": ["FN1", "FN3"],
                    "code": code,
                    "start_time": start,
                    "end_time": end,
                    "report_type": "tag_time",
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[tdx-corporate] %s 财务取数失败: %s", code, e)
            return None

    resp = _cached(("FN13", code, start, end), _load)
    if not isinstance(resp, dict):
        return {"available": False, "items": [], "source": SOURCE, "note": "通达信取数失败"}
    eps = resp.get("FN1") or []
    undist = resp.get("FN3") or []
    dates = resp.get("announce_time") or resp.get("tag_time") or []
    items = []
    for i, d in enumerate(dates):
        items.append(
            {
                "date": str(d),
                "eps": _num(eps[i]) if i < len(eps) else None,
                "undistributed_ps": _num(undist[i]) if i < len(undist) else None,
            }
        )
    return {
        "available": True,
        "items": items[-8:],
        "count": len(items),
        "source": SOURCE,
        "caliber": "TQ 专业财务 FN1(每股收益)/FN3(每股未分配利润)，单位元，日期=公告日",
        "note": None if items else "该股无可用财务指标（TQ 口径）",
    }


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def dossier(symbol: str, *, days: int = 180) -> dict:
    """一次取齐五类（供个股基本面/对话工具复用）。单类失败不影响其他类。"""
    out = {"symbol": str(symbol or ""), "source": "tdx_tq"}
    for key, fn, kw in (
        ("block_trades", block_trades, {"days": max(days, 365)}),
        ("holder_changes", holder_changes, {"days": max(days, 730)}),
        ("northbound", northbound_holding, {"days": 180}),
        ("pledge", pledge, {"days": max(days, 730)}),
        ("financials", financials, {}),
    ):
        try:
            out[key] = fn(symbol, **kw)
        except Exception as e:  # noqa: BLE001
            logger.warning("[tdx-corporate] %s %s 失败: %s", symbol, key, e)
            out[key] = {"available": False, "items": [], "source": SOURCE, "note": f"取数异常: {e}"}
    return out

def _d2(v) -> str:
    """两位小数（价格/每股指标不能用整数格式化，35.57 变 36 是失真）。"""
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


def render_lines(corp: dict, fmt_num, fmt_yi) -> list[str]:
    """把 dossier 结果渲染成对话文本行（放在这里而不是 chat.py：那边有行数护栏）。"""
    out: list[str] = []
    corp = corp or {}
    bt = corp.get("block_trades") or {}
    if bt.get("available"):
        out.append(f"■ 大宗交易: {bt.get('count', 0)}笔" if bt.get("count")
                   else "■ 大宗交易: 近一年无记录(通达信口径)")
        for r in (bt.get("items") or [])[-5:]:
            out.append(f"- {r.get('date')} 均价{_d2(r.get('price'))}元 "
                       f"成交{fmt_yi(r.get('amount'))}亿")
    hc = corp.get("holder_changes") or {}
    if hc.get("available"):
        out.append(f"■ 增减持: {hc.get('count', 0)}次" if hc.get("count")
                   else "■ 增减持: 近两年无记录(通达信口径)")
        for r in (hc.get("items") or [])[-5:]:
            out.append(f"- {r.get('date')} 均价{_d2(r.get('price'))}元 "
                       f"变动{fmt_num(r.get('shares'))}股")
    nb = corp.get("northbound") or {}
    if nb.get("available") and nb.get("latest"):
        lt = nb["latest"]
        delta = nb.get("delta")
        d_str = f"{delta:+,.0f}股" if isinstance(delta, (int, float)) else "—"
        out.append(f"■ 陆股通持股: {lt.get('date')} {fmt_num(lt.get('shares'))}股 "
                   f"(较上次 {d_str})")
    pl = corp.get("pledge") or {}
    if pl.get("available") and pl.get("latest"):
        lt = pl["latest"]
        out.append(f"■ 股权质押: {lt.get('date')} 比例{_d2(lt.get('ratio_pct'))}% "
                   f"数量{fmt_num(lt.get('shares_wan'))}万股")
    fin = corp.get("financials") or {}
    if fin.get("available") and fin.get("items"):
        last = fin["items"][-1]
        out.append(f"■ 财务(TQ专业财务): {last.get('date')} "
                   f"每股收益{_d2(last.get('eps'))}元 "
                   f"每股未分配{_d2(last.get('undistributed_ps'))}元")
    return out
