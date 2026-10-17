"""可转债 - 正股联动套利与折溢价雷达(I-4, 2026-10-18)。

审计背景(`docs/research/SIDA_PRO整体项目深度审计与创新功能规划.md` §4.5 / A-08):
后端只有 1 个 `get_kzz_terms` chat 工具与 thsdk 转债行情, **前端 0 引用**; `get_kzz_info`
给出的转股价 / 强赎触发价 / 纯债价值 / 评级等 14 个字段没有任何页面消费。
本模块把这些条款数据接成产品能力: 把转债当作"正股事件的二阶导数"来监控。

指标(**全部由通达信给的一手字段推导, 不自造口径**):

```
转股价值 = 100 / 转股价 × 正股现价
溢价率   = 转债价 / 转股价值 - 1
双低值   = 转债价 + 溢价率×100
强赎距离 = (正股价 - 强赎触发价) / 强赎触发价
纯债溢价 = 转债价 / 纯债价值 - 1
```

铁律(与 A-06 同一课):
- `RealValue`(纯债价值) / `ForceRedeem`(强赎触发价) **直接取客户端**, 不自己按贴现率估算
  —— 一旦与客户端数字不同, 用户只会认为"这系统数据是假的"。
- 任何输入缺失 → 该指标 `None` + 明确原因, **绝不填 0 或估算值**。
- 场景判定只陈述可观测事实(折价 / 溢价 / 距离触发价多少), 不写买卖建议。

数据源(零新增依赖):
- `tq.stock_list(market="5", list_type=32)` → 可转债代码表
- `tq.pricevol(codes)` → 批量现价/昨收/涨幅(500 只/片)
- `tq.kzz_info(code)` → 条款(get_kzz_info, 必须带后缀)
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime

logger = logging.getLogger(__name__)

#: 条款缓存 TTL: 条款只在分红/下修/强赎公告时变, 12h 足够; 缓存是为了把
#: "每只转债一次 RPC" 的成本摊到冷启动一次(见 A-02 的教训: N+1 不能挂在请求主路径上)。
_TERMS_TTL = 12 * 3600.0
_terms_cache: dict[str, tuple[float, dict]] = {}

#: 单次响应最多算多少只(冷启动 N 次 kzz_info RPC)。按|涨跌幅|取 Top-N,
#: 因为用户真正会看的就是异动/折价的那几十只, 而不是全市场 500 只的平均值。
DEFAULT_LIMIT = 60
MAX_LIMIT = 200

_SCENARIO_LABELS = {
    "stock_limit_up_lag": "正股封板·转债滞后",
    "discount_arb": "折价套利",
    "force_redeem_breached": "强赎触发区",
    "force_redeem_near": "接近强赎",
    "double_low_top": "双低前列",
}


def clear_terms_cache() -> None:
    _terms_cache.clear()


# ── 纯计算层(无网络, 单测直接喂 dict) ──────────────────────────────────────
def _f(v) -> float | None:
    """客户端字段 → float; 空值/占位串一律 None(不把 0 当有效值)。"""
    if v is None or v == "" or v == "--":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def limit_up_threshold(code: str) -> float:
    """涨停幅度阈值(%): 创业板/科创板 20cm, 其余 10cm。北交所 30cm。"""
    c = (code or "").strip()
    if c.startswith(("300", "301", "688", "689")):
        return 19.8
    if c.startswith(("8", "4", "9")):
        return 29.8
    return 9.8


def compute_metrics(
    *,
    bond_price: float | None,
    bond_prev_close: float | None,
    conv_price: float | None,
    stock_price: float | None,
    stock_prev_close: float | None,
    force_redeem: float | None,
    pure_bond: float | None,
) -> dict:
    """由一手字段推导全部指标。任何输入缺失 → 对应字段 None(附 `missing` 说明)。"""
    missing: list[str] = []

    conv_value = None
    conv_value_prev = None
    if conv_price and conv_price > 0 and stock_price and stock_price > 0:
        conv_value = round(100.0 / conv_price * stock_price, 4)
    else:
        missing.append("conv_value")
    if conv_price and conv_price > 0 and stock_prev_close and stock_prev_close > 0:
        conv_value_prev = round(100.0 / conv_price * stock_prev_close, 4)

    premium_pct = None
    if bond_price and conv_value and conv_value > 0:
        premium_pct = round((bond_price / conv_value - 1.0) * 100.0, 2)
    else:
        missing.append("premium_pct")

    double_low = None
    if bond_price is not None and premium_pct is not None:
        double_low = round(bond_price + premium_pct, 2)

    bond_chg_pct = None
    if bond_price and bond_prev_close and bond_prev_close > 0:
        bond_chg_pct = round((bond_price / bond_prev_close - 1.0) * 100.0, 2)

    conv_chg_pct = None
    if conv_value and conv_value_prev and conv_value_prev > 0:
        conv_chg_pct = round((conv_value / conv_value_prev - 1.0) * 100.0, 2)

    # 转债相对自身转股价值的**滞后幅度**(正股涨停而转债没跟上时为正)
    lag_pct = None
    if conv_chg_pct is not None and bond_chg_pct is not None:
        lag_pct = round(conv_chg_pct - bond_chg_pct, 2)

    stock_chg_pct = None
    if stock_price and stock_prev_close and stock_prev_close > 0:
        stock_chg_pct = round((stock_price / stock_prev_close - 1.0) * 100.0, 2)

    redeem_distance: float | None = None
    if force_redeem and force_redeem > 0 and stock_price and stock_price > 0:
        redeem_distance = round((stock_price / force_redeem - 1.0) * 100.0, 2)
    else:
        missing.append("redeem_distance")

    pure_bond_premium = None
    if pure_bond and pure_bond > 0 and bond_price:
        pure_bond_premium = round((bond_price / pure_bond - 1.0) * 100.0, 2)
    else:
        missing.append("pure_bond_premium")

    return {
        "bond_price": bond_price,
        "bond_chg_pct": bond_chg_pct,
        "conv_price": conv_price,
        "conv_value": conv_value,
        "conv_chg_pct": conv_chg_pct,
        "premium_pct": premium_pct,
        "double_low": double_low,
        "stock_price": stock_price,
        "stock_chg_pct": stock_chg_pct,
        "lag_pct": lag_pct,
        "force_redeem_price": force_redeem,
        "redeem_distance_pct": redeem_distance,
        "pure_bond_value": pure_bond,
        "pure_bond_premium_pct": pure_bond_premium,
        "missing": missing,
    }


def classify_scenarios(row: dict) -> list[dict]:
    """把指标翻成"可交易场景"卡片。只陈述条件是否成立, 不给买卖建议。"""
    out: list[dict] = []
    stock_code = str(row.get("stock_code") or "")
    stock_chg = row.get("stock_chg_pct")
    lag = row.get("lag_pct")
    premium = row.get("premium_pct")
    dist = row.get("redeem_distance_pct")

    # ① 正股封板但转债没跟上(补涨博弈的信息基础)
    if (
        isinstance(stock_chg, (int, float))
        and stock_chg >= limit_up_threshold(stock_code)
        and isinstance(lag, (int, float))
        and lag >= 3.0
    ):
        out.append({
            "key": "stock_limit_up_lag",
            "label": _SCENARIO_LABELS["stock_limit_up_lag"],
            "tone": "opportunity",
            "detail": f"正股涨停 {stock_chg:+.2f}%，转股价值 {row.get('conv_chg_pct'):+.2f}%，转债仅 {row.get('bond_chg_pct'):+.2f}%（滞后 {lag:.2f} 个点）",
        })

    # ② 折价: 转股即赚(尚未计交易成本与 T+1 风险)
    if isinstance(premium, (int, float)) and premium <= -1.0:
        out.append({
            "key": "discount_arb",
            "label": _SCENARIO_LABELS["discount_arb"],
            "tone": "opportunity",
            "detail": f"溢价率 {premium:.2f}%（债价低于转股价值，转股存在价差；未计费用与次日风险）",
        })

    # ③ 强赎: 硬约束(一旦公告, 转债价格向转股价值收敛)
    if isinstance(dist, (int, float)):
        if dist >= 0:
            out.append({
                "key": "force_redeem_breached",
                "label": _SCENARIO_LABELS["force_redeem_breached"],
                "tone": "risk",
                "detail": f"正股 {row.get('stock_price')} ≥ 强赎触发价 {row.get('force_redeem_price')}（{dist:+.2f}%），需关注发行人强赎公告",
            })
        elif dist >= -10:
            out.append({
                "key": "force_redeem_near",
                "label": _SCENARIO_LABELS["force_redeem_near"],
                "tone": "watch",
                "detail": f"距强赎触发价还差 {abs(dist):.2f}%",
            })

    if row.get("double_low_top"):
        out.append({
            "key": "double_low_top",
            "label": _SCENARIO_LABELS["double_low_top"],
            "tone": "info",
            "detail": f"双低值 {row.get('double_low')}（全池第 {row.get('double_low_rank')} 低）",
        })
    return out


def _scenario_priority(row: dict) -> int:
    keys = {s["key"] for s in row.get("scenarios") or []}
    if "force_redeem_breached" in keys:
        return 0
    if "discount_arb" in keys:
        return 1
    if "stock_limit_up_lag" in keys:
        return 2
    if "force_redeem_near" in keys:
        return 3
    return 4


def build_rows(
    *,
    terms: dict[str, dict],
    bond_quotes: dict[str, dict],
    stock_quotes: dict[str, dict],
    double_low_top_n: int = 20,
) -> list[dict]:
    """条款 + 债/股报价 → 行列表(纯计算)。

    `terms` / `bond_quotes` / `stock_quotes` 的键都是 **6 位代码**。
    """
    rows: list[dict] = []
    for code, t in (terms or {}).items():
        if not isinstance(t, dict) or not t:
            continue
        bq = (bond_quotes or {}).get(code) or {}
        stock_code = str(t.get("_stock_code") or "")
        sq = (stock_quotes or {}).get(stock_code) or {}

        bond_price = _f(bq.get("Now"))
        if bond_price in (None, 0.0):
            bond_price = _f(t.get("KZZNow"))  # 条款接口自带现价, 仅作兜底
        m = compute_metrics(
            bond_price=bond_price,
            bond_prev_close=_f(bq.get("LastClose")),
            conv_price=_f(t.get("ZGPrice")),
            stock_price=_f(sq.get("Now")),
            stock_prev_close=_f(sq.get("LastClose")),
            force_redeem=_f(t.get("ForceRedeem")),
            pure_bond=_f(t.get("RealValue")),
        )
        rows.append({
            "code": code,
            "name": str(t.get("KZZName") or t.get("KZZCode") or code),
            "stock_code": stock_code,
            "stock_name": str(t.get("HSName") or ""),
            "rating": t.get("HSScore"),
            "rest_scope_raw": t.get("RestScope"),
            "end_date": t.get("EndDate"),
            "convert_start": t.get("ZGDate"),
            "put_back_price": _f(t.get("PutBack")),
            "cur_rate": _f(t.get("CurRate")),
            **m,
            "scenarios": [],
        })

    # 双低排名: 只在有 double_low 的池子里排, 不足 N 只就全给
    ranked = sorted(
        [r for r in rows if isinstance(r.get("double_low"), (int, float))],
        key=lambda r: r["double_low"],
    )
    for i, r in enumerate(ranked[: max(0, int(double_low_top_n))], start=1):
        r["double_low_rank"] = i
        r["double_low_top"] = True

    for r in rows:
        r["scenarios"] = classify_scenarios(r)
        r["scenario_keys"] = [s["key"] for s in r["scenarios"]]

    rows.sort(
        key=lambda r: (
            _scenario_priority(r),
            r["premium_pct"] if isinstance(r.get("premium_pct"), (int, float)) else 999.0,
            -abs(r.get("bond_chg_pct") or 0.0),
        )
    )
    return rows


# ── 数据接入层(全部走 TQ, 失败如实降级) ──────────────────────────────────
def _tq_code(code: str) -> str:
    """6 位代码 → 通达信带后缀代码。

    ⚠️ `to_tq_code` 的交易所规则面向股票(6/9/5→SH), 对**转债不成立**:
    110/111/113 全在上交所却被判成 .SZ, 于是 kzz_info 首次调用必然失败。
    转债按号段单独判, 其余交给产品统一规则。
    """
    c = (code or "").strip().upper()
    if c[:2] in ("11", "13"):
        return f"{c}.SH"
    if c[:2] == "12":
        return f"{c}.SZ"
    from marketdata.symbol import Symbol
    from marketdata.vendors.tq import to_tq_code

    return to_tq_code(Symbol.parse(c, "CN")) or c


def _universe() -> list[str]:
    """全市场可转债代码表(list_type=32)。"""
    from marketdata.vendors.tq import stock_list

    raw = stock_list(market="5", list_type=32) or []
    out: list[str] = []
    for item in raw:
        code = ""
        if isinstance(item, str):
            code = item
        elif isinstance(item, dict):
            for k in ("code", "Code", "SECURITY_CODE", "证券代码", "stock_code"):
                if item.get(k):
                    code = str(item[k])
                    break
        elif isinstance(item, (list, tuple)) and item:
            code = str(item[0])
        code = code.strip().upper().replace("SH", "").replace("SZ", "").replace(".", "")
        if code.isdigit() and len(code) == 6 and code not in out:
            out.append(code)
    return out


def _batch_quotes(codes: list[str], *, chunk: int = 500) -> dict[str, dict]:
    """批量价量(chunked)。返回 {6位代码: {Now, LastClose, Zaf}}; 未知字段原样保留。"""
    from marketdata.vendors.tq import pricevol

    out: dict[str, dict] = {}
    codes = [c for c in codes if c]
    for i in range(0, len(codes), chunk):
        part = codes[i : i + chunk]
        try:
            got = pricevol([_tq_code(c) for c in part]) or {}
        except Exception as e:  # noqa: BLE001 - 单批失败不拖垮整轮
            logger.warning("[cb_radar] pricevol 批次失败(%d 只): %r", len(part), e)
            continue
        for k, v in got.items():
            key = str(k).strip().upper().replace("SH", "").replace("SZ", "").replace(".", "")
            if key.isdigit() and isinstance(v, dict):
                out[key] = v
    return out


def _terms(code: str) -> dict:
    """条款(带 12h 进程内缓存)。失败返回 {}(调用方按"无条款"处理, 不猜)。"""
    now = time.time()
    hit = _terms_cache.get(code)
    if hit and now - hit[0] < _TERMS_TTL:
        return hit[1]
    from marketdata.vendors.tq import kzz_info

    data: dict = {}
    for cand in (_tq_code(code), f"{code}.SZ", f"{code}.SH"):
        try:
            got = kzz_info(cand)
        except Exception as e:  # noqa: BLE001 - 单只失败不影响整池
            logger.debug("[cb_radar] kzz_info %s 失败: %r", cand, e)
            continue
        if isinstance(got, dict) and got:
            data = got
            break
    if data:
        _terms_cache[code] = (now, data)
    return data


def _stock_code_of(term: dict) -> str:
    raw = str(term.get("HSCode") or term.get("HSName") or "").strip()
    if not raw:
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else ""


def _date_meta(meta: dict, now: datetime | None) -> dict:
    """数据日 / 是否实时(与竞价池同一套语义, 见 src/core/trade_phase.py)。"""
    from src.core.trade_phase import PHASE_AUCTION, PHASE_CONTINUOUS, cn_now
    from src.core.trading_calendar import trading_day_anchor

    today = cn_now(now).date()
    data_date = trading_day_anchor(today)
    live = meta.get("trade_phase") in (PHASE_AUCTION, PHASE_CONTINUOUS) and data_date == today
    return {"data_date": data_date.isoformat(), "is_live": live, "is_today": data_date == today}


def build_radar(*, limit: int = DEFAULT_LIMIT, now: datetime | None = None) -> dict:
    """组装雷达响应。**永不抛**: 数据源不可用 → available=False + 可诊断 note。"""
    from src.core.trade_phase import as_of_iso, phase_meta

    t0 = time.time()
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    meta = phase_meta(now)

    try:
        universe = _universe()
    except Exception as e:  # noqa: BLE001
        logger.warning("[cb_radar] 转债代码表拉取失败: %r", e)
        return _degraded("转债代码表不可用（TQ get_stock_list list_type=32 失败）", meta, e)

    if not universe:
        return _degraded("通达信未返回可转债列表（客户端未下载转债数据或网关不可用）", meta)

    bond_quotes = _batch_quotes(universe)
    if not bond_quotes:
        return _degraded("转债批量行情不可用（TQ get_pricevol 无返回）", meta)

    # Top-N: 按|涨跌幅|挑选, 没有报价的排后面 —— 用户真正会看的是异动的那几十只
    def _rank(c: str) -> float:
        q = bond_quotes.get(c) or {}
        zaf = _f(q.get("Zaf"))
        return abs(zaf) if zaf is not None else -1.0

    picked = sorted(universe, key=_rank, reverse=True)[:limit]

    terms: dict[str, dict] = {}
    for code in picked:
        t = _terms(code)
        if not t:
            continue
        t = dict(t)
        t["_stock_code"] = _stock_code_of(t)
        terms[code] = t

    if not terms:
        return _degraded("转债条款不可用（TQ get_kzz_info 全部失败）", meta)

    stock_codes = sorted({t["_stock_code"] for t in terms.values() if t.get("_stock_code")})
    stock_quotes = _batch_quotes(stock_codes) if stock_codes else {}

    rows = build_rows(terms=terms, bond_quotes=bond_quotes, stock_quotes=stock_quotes)

    counts: dict[str, int] = {}
    for r in rows:
        for s in r.get("scenarios") or []:
            counts[s["key"]] = counts.get(s["key"], 0) + 1

    return {
        "available": True,
        **_date_meta(meta, now),
        "trade_phase": meta["trade_phase"],
        "phase_label": meta["phase_label"],
        "as_of": as_of_iso(now),
        "source": "通达信 TQ: get_stock_list(可转债) + get_pricevol(批量) + get_kzz_info(条款)",
        "caliber": "转股价/强赎触发价/纯债价值均为客户端原值，未自行贴现或估算",
        "universe": len(universe),
        "evaluated": len(rows),
        "limit": limit,
        "rows": rows,
        "scenario_counts": counts,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "note": None,
    }


def build_one(code: str, *, now: datetime | None = None) -> dict:
    """单只转债详情(条款 + 债/股报价 + 指标 + 场景)。供 `/bond/{code}` 用。"""
    from src.core.trade_phase import as_of_iso, phase_meta

    meta = phase_meta(now)
    c = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(c) != 6:
        return {**_degraded(f"非法转债代码: {code!r}(需要 6 位)", meta), "code": code}

    t = _terms(c)
    if not t:
        return {**_degraded(f"通达信查不到转债 {c} 的条款(get_kzz_info)", meta), "code": c}
    t = dict(t)
    t["_stock_code"] = _stock_code_of(t)

    bond_quotes = _batch_quotes([c])
    stock_quotes = _batch_quotes([t["_stock_code"]]) if t["_stock_code"] else {}
    rows = build_rows(terms={c: t}, bond_quotes=bond_quotes, stock_quotes=stock_quotes)
    if not rows:
        return {**_degraded(f"转债 {c} 指标无法计算(条款字段缺失)", meta), "code": c}
    return {
        "available": True,
        **_date_meta(meta, now),
        "trade_phase": meta["trade_phase"],
        "phase_label": meta["phase_label"],
        "as_of": as_of_iso(now),
        "source": "通达信 TQ: get_kzz_info(条款) + get_pricevol(批量)",
        "caliber": "转股价/强赎触发价/纯债价值均为客户端原值，未自行贴现或估算",
        "row": rows[0],
        "note": None,
    }


def _degraded(note: str, meta: dict, err: Exception | None = None) -> dict:
    from src.core.trade_phase import as_of_iso

    return {
        # 没有任何可评估数据时**不给数据日**(避免"无数据却标了一个日期")
        "available": False,
        "data_date": None,
        "is_live": False,
        "is_today": False,
        "trade_phase": meta.get("trade_phase"),
        "phase_label": meta.get("phase_label"),
        "as_of": as_of_iso(),
        "source": "通达信 TQ",
        "caliber": "条款/行情缺失时如实降级，不估算、不填充默认值",
        "universe": 0,
        "evaluated": 0,
        "rows": [],
        "scenario_counts": {},
        "note": note + (f"（{type(err).__name__}: {err}）" if err else ""),
    }
