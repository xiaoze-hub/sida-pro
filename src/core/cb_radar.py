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
JOB_KIND = "cb_daily"
JOB_LABEL = "可转债条款+折溢价日终落库"


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
    """条款: **先读库(cb_terms)**, 未命中/过期才回源 kzz_info 并落库。

    改造动机(2026-10-18 P0-a): 原实现只有 12h 进程内 dict —— worker 重启/容器迁移
    即全空, Top-60 要串行打 60 次 `kzz_info`, 首屏秒开无从谈起(方案 §5.3)。
    现在进程内缓存只是 L1, 真正的 L0 是 `cb_terms`: 冷启动也零 RPC。
    """
    now = time.time()
    hit = _terms_cache.get(code)
    if hit and now - hit[0] < _TERMS_TTL:
        return hit[1]

    from src.core import cb_radar_store as store

    db_raw: dict = {}
    try:
        from src.db.session import SessionLocal

        with SessionLocal() as session:
            db_raw = (store.load_terms(session, [code]) or {}).get(code) or {}
    except Exception as e:  # noqa: BLE001 - 库不可用不能连累取数
        logger.debug("[cb_radar] 条款读库失败 %s: %r", code, e)

    if db_raw and store.terms_are_fresh(db_raw):
        _terms_cache[code] = (now, db_raw)
        return db_raw

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
        try:
            from src.db.session import SessionLocal

            with SessionLocal() as session:
                store.upsert_terms(session, code, data)
        except Exception as e:  # noqa: BLE001 - 落库失败不阻断本次响应
            logger.warning("[cb_radar] 条款落库失败 %s: %r", code, e)
        return data
    # 回源失败但有过期旧条款: 用旧条款(调用方从 _as_of 可判断新鲜度), 不返回空
    if db_raw:
        _terms_cache[code] = (now, db_raw)
        return db_raw
    return {}


def _prime_terms(codes: list[str]) -> None:
    """一次性把 `cb_terms` 里的新鲜条款灌进进程缓存(单次 IN 查询)。

    没有它: 进程重启后 Top-60 会逐个 `_terms()` → 60 次库查询(或 60 次 RPC)。
    有它: 1 次查询命中绝大多数, 只有真的缺条款的少数走 RPC。
    """
    now = time.time()
    missing = [
        c for c in codes
        if not (c in _terms_cache and now - _terms_cache[c][0] < _TERMS_TTL)
    ]
    if not missing:
        return
    try:
        from src.core import cb_radar_store as store
        from src.db.session import SessionLocal

        with SessionLocal() as session:
            rows = store.load_terms(session, missing)
    except Exception as e:  # noqa: BLE001 - 读库失败退回逐只 RPC(旧行为)
        logger.debug("[cb_radar] 条款批量预热失败: %r", e)
        return
    for code, raw in (rows or {}).items():
        if store.terms_are_fresh(raw):
            _terms_cache[code] = (now, raw)


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

    _prime_terms(picked)
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


def _cache_meta(*, hit: bool, source: str, age_s: float | None = None,
                ttl_s: int | None = None, stale: bool = False,
                reason: str | None = None,
                persist_error: str | None = None) -> dict:
    return {
        "hit": hit,
        "source": source,        # db | live | none
        "age_s": age_s,
        "ttl_s": ttl_s,
        "stale": stale,
        "reason": reason,
        "persist_error": persist_error,
    }


#: 转债快照 TTL(秒): 盘中行情在动, 读库行必须按新鲜度判; 非盘中数据冻结, 不设上限。
TTL_LIVE_S = 60


def _ttl_s(now: datetime | None = None) -> int | None:
    """交易时段返回 TTL(秒); 非交易时段返回 None(=库行视为定稿, 不再回源)。"""
    try:
        from src.core.trade_phase import phase_meta

        return TTL_LIVE_S if phase_meta(now).get("is_live_session") else None
    except Exception:  # noqa: BLE001
        return TTL_LIVE_S


def _db_servable(db_payload: dict, now: datetime | None = None) -> bool:
    """库里的快照行是否可以**直接出**(不回源)。

    规则(与 price_distribution/看板口径一致):
      · 连续竞价/竞价撮合中: 必须是**今天**的行, 且 `age_s <= TTL_LIVE_S` ——
        否则盘中会把 10:00 的快照当成 11:00 的实时价(比"回源慢"更糟);
      · 其余时段(盘前/盘后/周末): 只要不早于上一交易日即可 —— 盘前出昨收是正确
        的"最新可得数据", 盘后若 15:20 的 job 已跑则出今日定稿。
    """
    from src.core.trade_phase import (
        PHASE_AUCTION, PHASE_AUCTION_MATCH, PHASE_CONTINUOUS, cn_now, phase_meta,
    )
    from src.core.trading_calendar import prev_trading_day

    td = str(db_payload.get("trade_date") or "")
    if not td:
        return False
    try:
        d = date.fromisoformat(td)
    except ValueError:
        return False
    today = cn_now(now).date().isoformat()
    if phase_meta(now).get("trade_phase") in (
        PHASE_AUCTION, PHASE_AUCTION_MATCH, PHASE_CONTINUOUS
    ):
        if td != today:
            return False
        age = db_payload.get("age_s")
        return age is not None and float(age) <= TTL_LIVE_S
    if td == today:
        return True
    try:
        return d >= prev_trading_day(cn_now(now).date())
    except Exception:  # noqa: BLE001 - 日历异常时保守回源
        return False


def _payload_from_db(db_payload: dict, *, limit: int, now: datetime | None = None) -> dict:
    """库行 → 与 `build_radar` **同形**的响应(前端零改动)。"""
    from src.core.trade_phase import as_of_iso, cn_now, phase_meta

    rows = db_payload.get("rows") or []
    counts: dict[str, int] = {}
    for r in rows:
        for key in r.get("scenario_keys") or []:
            counts[key] = counts.get(key, 0) + 1
    meta = phase_meta(now)
    td = str(db_payload.get("trade_date") or "")
    today = cn_now(now).date().isoformat()
    is_today = td == today
    universe = db_payload.get("universe")
    return {
        "available": True,
        "data_date": td,
        "is_live": False,          # 读库 = 定稿/采样值, 绝不冒充实时
        "is_today": is_today,
        "trade_phase": meta["trade_phase"],
        "phase_label": meta["phase_label"],
        "as_of": as_of_iso(now),
        "source": "通达信 TQ(已落库: cb_daily_snapshot)",
        "caliber": "转股价/强赎触发价/纯债价值均为客户端原值，未自行贴现或估算",
        "universe": int(universe) if universe else len(rows),
        "evaluated": len(rows),
        "limit": limit,
        "rows": rows,
        "scenario_counts": counts,
        "elapsed_ms": 0,
        "note": None if is_today else f"显示 {td} 收盘快照(当日快照尚未生成)",
    }


def get_radar(
    *,
    limit: int = DEFAULT_LIMIT,
    refresh: bool = False,
    now: datetime | None = None,
    persist: bool = True,
    builder=None,
    session_factory=None,
) -> dict:
    """读库优先 → 不可用则回源构建 → 落库 → 出参。API 层唯一入口。

    `refresh=True` 跳过读库(运维/调试), 但**仍会落库**; `persist=False` 只读不写
    (单测/只读副本)。`builder` / `session_factory` 供单测注入。
    """
    import time as _time

    from src.core import cb_radar_store as store

    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    t0 = _time.time()

    if not refresh:
        try:
            maker = session_factory or _session_factory()
            with maker() as session:
                db_payload = store.load_daily_rows(session, limit=limit)
        except Exception as e:  # noqa: BLE001 - 库不可用不能连累出数
            logger.warning("[cb_radar] 快照读库失败: %r", e)
            db_payload = None
        if db_payload and _db_servable(db_payload, now):
            out = _payload_from_db(db_payload, limit=limit, now=now)
            out["elapsed_ms"] = int((_time.time() - t0) * 1000)
            out["cached"] = True
            out["age_s"] = db_payload.get("age_s") or 0
            out["cache"] = _cache_meta(
                hit=True, source="db", age_s=db_payload.get("age_s"),
                ttl_s=_ttl_s(now),
            )
            return out

    build = builder or build_radar
    payload = build(limit=limit, now=now)

    persist_error = None
    persisted = 0
    if persist and payload.get("available") and payload.get("rows"):
        res = store.persist_radar(
            payload, session_factory=session_factory or _session_factory()
        )
        persisted = res.get("rows") or 0
        persist_error = res.get("error")

    out = dict(payload)
    out["cached"] = False
    out["age_s"] = 0
    out["cache"] = _cache_meta(
        hit=False,
        source="live" if payload.get("available") else "none",
        age_s=0.0 if payload.get("available") else None,
        reason=None if payload.get("available") else payload.get("note"),
        persist_error=persist_error,
        ttl_s=_ttl_s(now),
    )
    out["persisted_rows"] = persisted
    return out


def _session_factory():
    from src.db.session import SessionLocal

    return SessionLocal


def run_cb_daily_job(limit: int = MAX_LIMIT, now: datetime | None = None) -> dict:
    """cron 入口(交易日 15:20): 条款刷新 + 当日折溢价/双低快照定稿落库。

    走作业框架(`jobs.create/finish`), `ok=False` 会被判 failed —— 数据源不可用的
    日子绝不在面板上显示"成功但 0 行"。落库的最大 `limit` 用 `MAX_LIMIT`(200),
    保证读侧任何合法 limit 都能从库里截断。
    """
    from src.core.jobs import jobs
    from src.core.trade_phase import cn_now
    from src.core.trading_calendar import is_trading_day

    if not is_trading_day(cn_now(now).date()):
        # 非交易日行情冻结, 重跑只会把同一份收盘价重复 upsert(浪费 TQ RPC)
        return {"ok": True, "skipped": "non_trading_day", "rows": 0}

    job_id, is_new = jobs.create(JOB_KIND, JOB_LABEL)
    if not is_new:
        return {"ok": True, "skipped": "in_flight", "job_id": job_id}
    try:
        payload = build_radar(limit=min(int(limit or MAX_LIMIT), MAX_LIMIT), now=now)
        if not payload.get("available"):
            out = {
                "ok": False,
                "reason": payload.get("note") or "转债雷达不可用",
                "job_id": job_id,
                "rows": 0,
            }
            jobs.finish(job_id, out, context="可转债日终落库: ")
            return out
        from src.core import cb_radar_store as store

        res = store.persist_radar(payload)
        ok = bool(res.get("rows"))
        out = {
            "ok": ok,
            "reason": None if ok else (res.get("error") or "无可用行(债券价全缺失)"),
            "job_id": job_id,
            "trade_date": payload.get("data_date"),
            "evaluated": payload.get("evaluated"),
            "rows": res.get("rows") or 0,
        }
    except Exception as e:  # noqa: BLE001
        jobs.fail(job_id, str(e))
        logger.exception("可转债日终落库作业异常: %s", e)
        return {"ok": False, "error": str(e), "job_id": job_id}
    jobs.finish(job_id, out, context="可转债日终落库: ")
    return out


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
