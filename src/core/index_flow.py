"""指数盘面资金（2026-11-03）。

**为什么单独一个模块**：`get_exday_data` 对指数返回的是**成分股聚合的四档资金矩阵**，
与个股同一接口但语义不同（个股=该票自身的单笔分档；指数=全部成分股按单笔金额分档后求和）。
它比"主力净流入"多两层信息：四档结构分化，以及主动买/主动卖拆分。

口径（`caliber = tdx_index_agg`）：
- `Amo[行][列]`：行 = [特大, 大, 中, 小]，列 = [买入额, 卖出额, 主动买额, 主动卖额]，单位**元**；
- `main_net` = (特大+大) 的 (主动买 − 主动卖)，等价于"主力净流入"的可比量；
- `net_all` = 四档 (主动买 − 主动卖) 之和。

**三条恒等式**（`validate()`，任一不过标 `quality='suspect'`，数据照存不丢）：
1. Σ买入 == Σ卖出 == 当日成交额；
2. Σ主动买 + Σ主动卖 == 当日成交额；
3. (Σ主动买 − Σ主动卖) == 全单净额。

指数快照的两个单位坑：`Amount` 是**万元**（×1e4 才是元），`Volume` 是**手**。
"""

from __future__ import annotations

from typing import Any

CALIBER = "tdx_index_agg"
TIER_KEYS = ("xl", "lg", "md", "sm")

# 恒等式容差：四舍五入后实测最大 0.16 亿 / 8000 亿 ≈ 0.002%，留 0.5% 足够宽又不掩盖错位
_TOL = 0.005


def _int_or_none(v: Any) -> int | None:
    """整数化但保留真实的 0（小指数"全员下跌"时 UpHome=0 是有效值，不是缺失）。"""
    n = _num(v)
    return int(n) if n is not None else None


def _num(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _rpc(method: str, params: dict[str, Any], timeout: float = 6.0) -> Any:
    """`tq_rpc` **已经**做过 `result.get("Value", result)` 的解包（见
    `packages/marketdata/.../tq.py::_rpc`）：

    - `get_pricevol`   → `{code: {...}}`（Value 层）；
    - `get_market_snapshot` → **扁平 dict**（快照字段与 ErrorId 平级，无 result 包裹）；
    - `get_exday_data` → **list**（Value 层）。

    所以这里**不能再取 result/Value**，否则三处全部恒为空 —— 会静默写 0 行。
    `_unwrap()` 只做防御性兼容，正常路径直接返回原值。
    """
    from marketdata.vendors.tq import tq_rpc

    return tq_rpc(method, params, timeout=timeout)


def _unwrap(resp: Any) -> Any:
    """兼容仍带 `result`/`Value` 包裹的形态（正常路径原样返回）。"""
    if isinstance(resp, dict):
        if "result" in resp:
            resp = resp["result"]
        if isinstance(resp, dict) and "Value" in resp:
            resp = resp["Value"]
    return resp


def parse_tiers(amo: Any) -> list[list[float]] | None:
    """4×4 矩阵 → float 二维数组；形状不对返回 None（不猜）。"""
    if not isinstance(amo, list) or len(amo) != 4:
        return None
    out: list[list[float]] = []
    for row in amo:
        if not isinstance(row, list) or len(row) != 4:
            return None
        vals = [_num(x) for x in row]
        if any(v is None for v in vals):
            return None
        out.append([float(v) for v in vals if v is not None])
    return out


def validate(amo: list[list[float]], snapshot_amount_yuan: float | None = None) -> tuple[bool, str]:
    """三条恒等式 + 与快照成交额交叉核对；返回 (是否通过, 原因)。"""
    buy = sum(r[0] for r in amo)
    sell = sum(r[1] for r in amo)
    act_buy = sum(r[2] for r in amo)
    act_sell = sum(r[3] for r in amo)
    amount = buy

    if amount <= 0:
        return False, "成交额为 0（非交易日或客户端未取到）"
    if abs(buy - sell) / amount > _TOL:
        return False, f"买卖两侧不等: {buy:.0f} vs {sell:.0f}"
    if abs(act_buy + act_sell - amount) / amount > _TOL:
        return False, "主动买+主动卖 ≠ 成交额"
    if snapshot_amount_yuan and abs(snapshot_amount_yuan - amount) / amount > 0.05:
        return False, (
            f"与快照成交额不符: 快照 {snapshot_amount_yuan / 1e8:.1f}亿 vs 四档 {amount / 1e8:.1f}亿"
        )
    return True, ""


def summarize(amo: list[list[float]]) -> dict[str, float]:
    """四档矩阵 → 落库字段。"""
    net = {k: amo[i][2] - amo[i][3] for i, k in enumerate(TIER_KEYS)}
    act_buy = sum(r[2] for r in amo)
    act_sell = sum(r[3] for r in amo)
    return {
        "amount": sum(r[0] for r in amo),
        "active_buy": act_buy,
        "active_sell": act_sell,
        "xl_net": net["xl"],
        "lg_net": net["lg"],
        "md_net": net["md"],
        "sm_net": net["sm"],
        "main_net": net["xl"] + net["lg"],
        "net_all": act_buy - act_sell,
    }


def quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """批量涨跌幅（`get_pricevol`，一次多票）；失败返回空 dict（调用方标未知）。"""
    codes = []
    from src.core.index_codes import tq_index_code

    for s in symbols:
        c = tq_index_code(s)
        if c:
            codes.append(c)
    if not codes:
        return {}
    try:
        resp = _rpc("get_pricevol", {"stock_list": codes}) or {}
    except Exception:  # noqa: BLE001 - 取数失败由上游标"未知"
        return {}
    value = _unwrap(resp)
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for code, d in value.items():
        if not isinstance(d, dict):
            continue
        out[str(code).split(".")[0]] = {
            "close": _num(d.get("Now")),
            "prev_close": _num(d.get("LastClose")),
            "pct_chg": _num(d.get("Zaf")),
            "volume_lots": _num(d.get("Volume")),
        }
    return out


def snapshot(code: str) -> dict[str, Any]:
    """单指数快照：涨跌家数 / 成交额(万元→元) / 均价 / 内外盘。失败返回 {}。"""
    try:
        resp = _rpc("get_market_snapshot", {"stock_code": code}) or {}
    except Exception:  # noqa: BLE001
        return {}
    d = _unwrap(resp)
    if not isinstance(d, dict) or str(d.get("ErrorId", "0")) != "0":
        return {}
    amount_wan = _num(d.get("Amount"))
    return {
        # `Now` = 最新价；`Average` 是**当日均价(VWAP)**，绝不可当收盘价用
        "close": _num(d.get("Now")),
        "amount": amount_wan * 1e4 if amount_wan is not None else None,
        "volume_lots": _num(d.get("Volume")),
        "open": _num(d.get("Open")),
        "high": _num(d.get("Max")),
        "low": _num(d.get("Min")),
        "avg_price": _num(d.get("Average")),
        "up_home": _int_or_none(d.get("UpHome")),
        "down_home": _int_or_none(d.get("DownHome")),
        "outside": _num(d.get("Outside")),
        "inside": _num(d.get("Inside")),
    }


def tiers(code: str) -> dict[str, Any] | None:
    """单指数四档资金；无数据返回 None。"""
    try:
        resp = _rpc("get_exday_data", {"stock_code": code, "count": 1}) or {}
    except Exception:  # noqa: BLE001
        return None
    rows = _unwrap(resp)
    if not isinstance(rows, list) or not rows:
        return None
    r = rows[-1]
    amo = parse_tiers(r.get("Amo"))
    if amo is None:
        return None
    return {"amo": amo, "trade_date": str(r.get("Date") or "")}


def fetch_one(symbol: str, quote: dict[str, Any] | None = None) -> dict[str, Any]:
    """单指数完整一行；不可用时 `available=False` + `reason`（不补 0）。"""
    from src.core.index_codes import index_name, tq_index_code

    sym = str(symbol or "").strip()[:6]
    code = tq_index_code(sym)
    base: dict[str, Any] = {
        "symbol": sym,
        "name": index_name(sym),
        "caliber": CALIBER,
        "source": "tq",
        "available": False,
        "reason": "",
    }
    if not code:
        base["reason"] = f"非指数代码: {sym!r}"
        return base

    snap = snapshot(code)
    t = tiers(code)
    if t is None:
        base["reason"] = "TQ 无当日四档数据（非交易日/客户端未开）"
        return base

    amo = t["amo"]
    ok, why = validate(amo, snap.get("amount"))
    row = summarize(amo)
    q = quote if quote is not None else quotes([sym]).get(sym, {})

    # 收盘价只认现价：快照 `Average` 是 VWAP（当日均价），
    # 拿它当 close 会把"均价相对昨收"算成涨跌幅 —— 静默错数。
    close = q.get("close")
    if close is None:
        close = snap.get("close")
    prev = q.get("prev_close")
    pct = q.get("pct_chg")
    if pct is None and close is not None and prev:
        pct = (close - prev) / prev * 100

    base.update(
        {
            "available": True,
            "trade_date": t["trade_date"] or "",
            "close": close,
            "prev_close": prev,
            "pct_chg": round(pct, 4) if pct is not None else None,
            "amount": row["amount"],
            "volume_lots": snap.get("volume_lots") or q.get("volume_lots"),
            "up_home": snap.get("up_home"),
            "down_home": snap.get("down_home"),
            "open": snap.get("open"),
            "high": snap.get("high"),
            "low": snap.get("low"),
            "outside": snap.get("outside"),
            "inside": snap.get("inside"),
            "active_buy": row["active_buy"],
            "active_sell": row["active_sell"],
            "xl_net": row["xl_net"],
            "lg_net": row["lg_net"],
            "md_net": row["md_net"],
            "sm_net": row["sm_net"],
            "main_net": row["main_net"],
            "net_all": row["net_all"],
            "quality": "ok" if ok else "suspect",
            "reason": why,
        }
    )
    return base


def fetch_many(symbols: list[str]) -> list[dict[str, Any]]:
    """批量取（涨跌幅一次拉全，其余逐只）。"""
    qs = quotes(symbols)
    return [fetch_one(s, qs.get(str(s)[:6])) for s in symbols]
