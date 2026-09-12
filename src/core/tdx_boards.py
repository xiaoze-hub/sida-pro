"""通达信客户端(TQ 网关)板块数据源 — 方案B(2026-09-10 老板拍板)。

链路: 容器 → `marketdata.vendors.tq.tq_rpc` → 本机 TdxW.exe HTTP(172.27.16.1:17709)。
与 thsdk(同花顺) 的关系: **板块目录口径不同且代码撞号不同义**(同花顺 881155=银行,
通达信 881155=其他纺织), 本模块以通达信原生目录为准(881xxx 二级行业 / 880xxx 概念),
不与其做代码映射。

能力(实测 2026-09-10 晚, 收盘后):
- 板块目录 get_sector_list(587, 排除 880081/880082 两个非板块指数) ~0.25s
- 板块/个股实时 get_pricevol 批量(587 码 84ms 容器内) → 现价/昨收/量
- 成交额/主力资金 公式批量(AMO: AMOW 万元; SUPAMO: 主力资金 万元) 各 ~0.3s
- 成分股 get_stock_list_in_sector; 全市场名称表 get_stock_list("5",1) → 5569 只 ~0.27s
- 涨速: 单码 get_market_snapshot 有 Zangsu 但 **不可批量**(230ms/码), 故由自身轮询价差自算
- 云数据(板块异动/轮动系数等 cfg_bk_bkld) 接口存在但需通达信数据权限, 当前返空 → 不用

⚠️ 陈旧快照防护: 客户端未更新时会"成功返回旧数据"(2026-09-03 生产事故同源), 调用方
必须用 data_fresh() 判定(基于板块日线最新日期)。
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

# 非板块的指数条目(轮动趋势/板块趋势), 不进热力图
EXCLUDED_SECTORS = {"880081.SH", "880082.SH"}

# A股市场号(TDX): market="5" 全A(list_type=1 带名称)
_A_MARKET = "5"
_A_SUFFIXES = (".SH", ".SZ", ".BJ")

_SECTOR_TTL_S = 300.0
_NAME_TTL_S = 12 * 3600.0
_FRESH_TTL_S = 60.0
_BATCH = 500
_DAILY_BATCH = 100
_SPEED_WINDOW_S = 300.0
_SPEED_TOL_S = 120.0
_TRADING_MINUTES = 240.0

_lock = threading.Lock()
_sector_cache: tuple[float, list[dict]] | None = None
_name_cache: tuple[float, dict[str, str]] | None = None
_fresh_cache: tuple[float, bool] | None = None
_baseline_cache: dict[str, object] = {}  # {"date": "2026-09-10", "amounts": {code: 元}}
_price_history: dict[str, list[tuple[float, float]]] = {}


def _clear_caches() -> None:
    """测试隔离/手动清理。"""
    global _sector_cache, _name_cache, _fresh_cache
    with _lock:
        _sector_cache = None
        _name_cache = None
        _fresh_cache = None
        _baseline_cache.clear()
        _price_history.clear()


def _rpc(method: str, params: dict, timeout: float = 6.0):
    """TQ JSON-RPC(单一网络缝, 测试打桩点)。"""
    from marketdata.vendors.tq import tq_rpc

    return tq_rpc(method, params, timeout=timeout)


def _num(v: object) -> float | None:
    if isinstance(v, (list, tuple)):
        v = v[-1] if v else None
    try:
        f = float(str(v).strip())
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    except Exception:  # noqa: BLE001
        return None


# ── 纯函数 ────────────────────────────────────────────────────────────────

def is_tdx_block_code(code: str) -> bool:
    c = (code or "").strip().upper()
    return len(c) == 9 and c.endswith(".SH") and c[:6].isdigit() and c[:2] == "88"


def block_type_of(code: str) -> str | None:
    c = (code or "").strip().upper()
    if not is_tdx_block_code(c):
        return None
    return "industry" if c.startswith("881") else "concept"


def tdx_code_to_symbol(code: str) -> str | None:
    """600150.SH → 600150; 仅 A股(SH/SZ/BJ), 其他(港股/美) 返回 None。"""
    c = (code or "").strip().upper()
    parts = c.split(".")
    if len(parts) != 2 or not parts[0].isdigit() or len(parts[0]) != 6:
        return None
    return parts[0] if f".{parts[1]}" in _A_SUFFIXES else None


def compute_speed(
    history: list[tuple[float, float]],
    now: float,
    window_s: float = _SPEED_WINDOW_S,
    tol_s: float = _SPEED_TOL_S,
) -> float | None:
    """涨速(%) = 现价 / window 分钟前价格 - 1, 基准样本须落在 [now-window±tol]。

    history = [(ts, price)] 升序; 样本不足/基准为 0 → None(不猜)。
    """
    if len(history) < 2:
        return None
    cur = history[-1]
    target = now - window_s
    best = None
    for ts, px in history[:-1]:
        if best is None or abs(ts - target) < abs(best[0] - target):
            best = (ts, px)
    if best is None or abs(best[0] - target) > tol_s:
        return None
    base = best[1]
    if not base:
        return None
    return (cur[1] / base - 1.0) * 100.0


def _chunks(seq: list[str], n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


# ── RPC 封装(带缓存) ──────────────────────────────────────────────────────

def sector_items() -> list[dict]:
    """通达信板块目录 [{code,name,board_type}] (排除非板块指数)。"""
    global _sector_cache
    now = time.time()
    with _lock:
        if _sector_cache and now - _sector_cache[0] < _SECTOR_TTL_S:
            return list(_sector_cache[1])
    raw = _rpc("get_sector_list", {"list_type": 1}) or []
    items: list[dict] = []
    for s in raw:
        code = str((s or {}).get("Code") or "").strip().upper()
        btype = block_type_of(code)
        if btype is None or code in EXCLUDED_SECTORS:
            continue
        items.append({"code": code, "name": str((s or {}).get("Name") or "").strip(), "board_type": btype})
    items.sort(key=lambda x: x["code"])
    with _lock:
        _sector_cache = (now, items)
    return list(items)


def name_map() -> dict[str, str]:
    """A股代码 → 名称(get_stock_list market=5 list_type=1, 5569 只, 缓存 12h)。"""
    global _name_cache
    now = time.time()
    with _lock:
        if _name_cache and now - _name_cache[0] < _NAME_TTL_S:
            return dict(_name_cache[1])
    raw = _rpc("get_stock_list", {"market": _A_MARKET, "list_type": 1}) or []
    names: dict[str, str] = {}
    for s in raw:
        if not isinstance(s, dict):
            continue
        code = str(s.get("Code") or "").strip().upper()
        if code.endswith(_A_SUFFIXES):
            names[code] = str(s.get("Name") or "").strip()
    with _lock:
        _name_cache = (now, names)
    return dict(names)


def data_fresh() -> bool:
    """通达信日线新鲜度(客户端未更新会成功返回旧数据 → 调用方据此判定)。"""
    return _probe().get("fresh", False)


def latest_trade_date() -> str | None:
    """通达信板块日线最新日期(YYYY-MM-DD); 探测失败 None(仅作展示基准)。"""
    return _probe().get("trade_date")


def _probe() -> dict:
    global _fresh_cache
    now = time.time()
    with _lock:
        if _fresh_cache and now - _fresh_cache[0] < _FRESH_TTL_S:
            return dict(_fresh_cache[1])
    info: dict = {"fresh": False, "trade_date": None}
    try:
        v = _rpc(
            "get_market_data",
            {"stock_list": ["881290.SH"], "period": "1d", "count": 1, "dividend_type": "front"},
        )
        rows = (v or {}).get("881290.SH") if isinstance(v, dict) else None
        dates = [str(d) for d in ((rows or {}).get("Date") or []) if str(d).strip()]
        if dates:
            last = max(dates)
            info["trade_date"] = f"{last[:4]}-{last[4:6]}-{last[6:8]}" if len(last) >= 8 else last
        from marketdata.vendors.tq import tq_bars_fresh

        info["fresh"] = bool(tq_bars_fresh(dates))
    except Exception as e:  # noqa: BLE001
        logger.warning("TDX 新鲜度探测失败: %s", e)
    with _lock:
        _fresh_cache = (now, dict(info))
    return info


def board_quotes(codes: list[str], *, with_fund: bool = True) -> dict[str, dict]:
    """批量实时: {code: {price, change_pct, volume, amount, fund_net}}。

    单源失败不阻断(对应字段 None); amount/fund_net 均已换算为元(源为万元)。
    """
    out: dict[str, dict] = {}
    if not codes:
        return out
    pv: dict = {}
    for part in _chunks(list(codes), _BATCH):
        try:
            got = _rpc("get_pricevol", {"stock_list": part}) or {}
            if isinstance(got, dict):
                pv.update(got)
        except Exception as e:  # noqa: BLE001
            logger.warning("TDX pricevol 失败(%d 码): %s", len(part), e)
    amounts: dict = {}
    funds: dict = {}
    for part in _chunks(list(codes), _BATCH):
        try:
            got = _rpc(
                "formula_process_mul_zb",
                {
                    "formula_name": "AMO",
                    "formula_arg": "",
                    "stock_list": part,
                    "stock_period": "1d",
                    "periodstr": "1d",
                    "count": -1,
                    "return_count": 1,
                    "dividend_type": 0,
                    "xsflag": -1,
                },
                timeout=20.0,
            )
            if isinstance(got, dict):
                amounts.update(got)
        except Exception as e:  # noqa: BLE001
            logger.warning("TDX AMO 公式失败(%d 码): %s", len(part), e)
        if not with_fund:
            continue
        try:
            got = _rpc(
                "formula_process_mul_zb",
                {
                    "formula_name": "SUPAMO",
                    "formula_arg": "",
                    "stock_list": part,
                    "stock_period": "1d",
                    "periodstr": "1d",
                    "count": -1,
                    "return_count": 1,
                    "dividend_type": 0,
                    "xsflag": -1,
                },
                timeout=20.0,
            )
            if isinstance(got, dict):
                funds.update(got)
        except Exception as e:  # noqa: BLE001
            logger.warning("TDX SUPAMO 公式失败(%d 码): %s", len(part), e)

    for code in codes:
        row: dict = {"price": None, "change_pct": None, "volume": None, "amount": None, "fund_net": None}
        p = pv.get(code) or {}
        now_px = _num(p.get("Now"))
        last = _num(p.get("LastClose"))
        row["price"] = now_px
        row["volume"] = _num(p.get("Volume"))
        if now_px is not None and last:
            row["change_pct"] = (now_px / last - 1.0) * 100.0
        am = _num((amounts.get(code) or {}).get("AMOW"))
        if am is not None:
            row["amount"] = am * 1e4
        fu = _num((funds.get(code) or {}).get("主力资金"))
        if fu is not None:
            row["fund_net"] = fu * 1e4
        out[code] = row
    return out


def pricevol_only(codes: list[str]) -> dict[str, dict]:
    """只拉 get_pricevol → {code: {price, last_close, open?, high?, low?}}。

    供连板梯队盘中扫描(v0.5.87): 状态机只需 price/last_close; 当日K 的 O/H/L 若原始报文
    带则透传(盘中成形K), 不带则调用方落 candle=None(不编影线)。单 chunk 失败跳过不抛。
    非交易时段 TDX 可能返回空 → 返回 {}(调用方降级)。
    """
    out: dict[str, dict] = {}
    for part in _chunks(list(codes), _BATCH):
        try:
            got = _rpc("get_pricevol", {"stock_list": part}) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("TDX pricevol_only 失败(%d 码): %s", len(part), e)
            continue
        for code, p in (got or {}).items():
            if not isinstance(p, dict):
                continue
            row = {"price": _num(p.get("Now")), "last_close": _num(p.get("LastClose"))}
            for src_key, dst in (("Open", "open"), ("High", "high"), ("Low", "low")):
                val = _num(p.get(src_key))
                if val is not None:
                    row[dst] = val
            out[code] = row
    return out


def constituents(block_code: str) -> list[str] | None:
    """板块成分股代码列表(失败 None)。"""
    try:
        v = _rpc("get_stock_list_in_sector", {"block_code": block_code, "block_type": 0})
    except Exception as e:  # noqa: BLE001
        logger.warning("TDX 成分股失败 %s: %s", block_code, e)
        return None
    if not isinstance(v, list):
        return None
    return [str(x).strip().upper() for x in v if str(x).strip()]


def amount_baseline(codes: list[str]) -> dict[str, float]:
    """每码"前 5 个交易日日均成交额(元)" — 当日缓存(盘中不变), 量比代理的基准。"""
    from datetime import date

    today = date.today().isoformat()
    cached = _baseline_cache.get("amounts")
    if _baseline_cache.get("date") == today and isinstance(cached, dict) and set(codes) <= set(cached):
        return {c: cached[c] for c in codes if c in cached}
    amounts: dict[str, float] = {}
    for part in _chunks(list(codes), _DAILY_BATCH):
        try:
            v = _rpc(
                "get_market_data",
                {"stock_list": part, "period": "1d", "count": 6, "dividend_type": "front"},
                timeout=20.0,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("TDX 日线批量失败(%d 码): %s", len(part), e)
            continue
        if not isinstance(v, dict):
            continue
        for code, rows in v.items():
            if not isinstance(rows, dict):
                continue
            amt = rows.get("Amount") or []
            # 日线 Amount 与 AMOW 同为万元 → ×1e4 归元(与今日额同口径, 否则量比虚高 1e4)
            vals = [x for x in (_num(a) for a in amt[:-1]) if x and x > 0]  # 去掉当日
            if vals:
                amounts[code] = (sum(vals) / len(vals)) * 1e4
    _baseline_cache["date"] = today
    _baseline_cache["amounts"] = amounts
    return {c: amounts[c] for c in codes if c in amounts}


def record_prices(quotes: dict[str, dict], now: float | None = None) -> None:
    """记录本次快照价格(自算涨速的样本序列; 保留 20 分钟)。"""
    ts = time.time() if now is None else now
    with _lock:
        for code, q in quotes.items():
            px = q.get("price")
            if px is None:
                continue
            hist = _price_history.setdefault(code, [])
            hist.append((ts, float(px)))
            if len(hist) > 200:
                del hist[:-200]


def speed_of(code: str, now: float | None = None) -> float | None:
    ts = time.time() if now is None else now
    with _lock:
        hist = list(_price_history.get(code) or [])
    return compute_speed(hist, ts)


def intraday_progress(now: float | None = None) -> float:
    """交易时段进度(0.05~1.0), 用于量比代理的时间归一。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    dt = datetime.fromtimestamp(time.time() if now is None else now, ZoneInfo("Asia/Shanghai"))
    minutes = dt.hour * 60 + dt.minute
    am_open, am_close = 9 * 60 + 30, 11 * 60 + 30
    pm_open, pm_close = 13 * 60, 15 * 60
    if minutes < am_open:
        elapsed = 0.0
    elif minutes <= am_close:
        elapsed = minutes - am_open
    elif minutes < pm_open:
        elapsed = 120.0
    elif minutes <= pm_close:
        elapsed = 120.0 + (minutes - pm_open)
    else:
        elapsed = _TRADING_MINUTES
    return max(0.05, min(1.0, elapsed / _TRADING_MINUTES))


def volume_ratio_proxy(today_amount: float | None, baseline: float | None, progress: float) -> float | None:
    """量比代理 = 今日成交额 / (前5日均额 × 时段进度); 基准缺/非正 → None。"""
    if today_amount is None or not baseline or baseline <= 0:
        return None
    expected = baseline * max(0.05, min(1.0, progress))
    if expected <= 0:
        return None
    return today_amount / expected
