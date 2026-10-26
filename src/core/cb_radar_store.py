# -*- coding: utf-8 -*-
"""可转债条款与每日折溢价的持久化 / 读穿透层(2026-10-18 P0-a, 方案 §7.2④)。

为什么要有这一层
----------------
改造前: 条款是 `cb_radar._terms_cache`(12h **进程内** dict), 快照只进
`biz_cache`(Redis 60s)。后果有两条, 恰好对应本轮用户诉求:

1. **冷启动 N 次 RPC**: worker 重启/容器迁移 → 缓存全空 → 每只转债打一次
   `kzz_info`(串行), Top-60 就是 60 次 RPC, 首屏秒开无从谈起;
2. **没有历史**: Redis 过期即删, 双低轮动/折价套利的历史净值永远算不出来。

本模块给出唯一数据通道(照抄 `price_distribution_store` 的范式):

    条款读: cb_terms 最近 as_of 行
             ├─ 命中且不过期 → 直接回吐 raw(零 RPC)
             └─ 缺失/过期     → 回源 kzz_info → upsert(cb_terms) → 回吐
    快照读: cb_daily_snapshot 最近交易日行(按 rank 取前 limit 行, 零 RPC)
    快照写: radar 每次成功构建后 upsert 当日全部已评估行(payload 原样)

诚实边界:
  · 条款字段缺失写 NULL, 不估算、不填 0;
  · 条款过期判断用 `as_of`(抓取日)而不是"现在有没有 RPC", 过期不影响出数,
    只是触发一次回源刷新 —— 回源失败时仍回吐旧行并标 `stale`;
  · 落库失败**不阻断**响应(返回本次现算结果 + `persist_error`), 但必须留日志;
  · 悟道/腾讯缺失的日子不写行: 读侧拿不到行就 `available=False`, 绝不拿 0 冒充。
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import CbDailySnapshot, CbTerms
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)

#: 条款有效期(天). 条款只在分红/下修/强赎公告时变, 但客户端字段可能随公告刷新;
#: 30 天足够保守 —— 过期只是"该刷新了", 不是"数据不可用"(见 load_terms)。
TERMS_MAX_AGE_DAYS = 30
#: 快照保留: 永久(按交易日一行, 500 只/日, 可忽略)。
TABLE_TERMS = "cb_terms"
TABLE_DAILY = "cb_daily_snapshot"


def _float(v) -> float | None:
    """客户端字段 → float。空值/占位串一律 None(不把 0 当有效值)。"""
    if v is None or v == "" or v == "--":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _iso_date(v: Any) -> str | None:
    """`YYYYMMDD` / `YYYY-MM-DD` / date / datetime → `YYYY-MM-DD`; 解析不了原样返回或 None。"""
    if v is None or v == "":
        return None
    if isinstance(v, (date,)):
        return v.isoformat()
    s = str(v).strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return s or None


def _stock_code_of(raw: dict) -> str:
    """正股代码: 优先 `HSCode`(可能带后缀), 退回 `HSName` 里的数字。"""
    for key in ("HSCode", "HSStockCode", "stock_code"):
        val = str(raw.get(key) or "").strip()
        digits = "".join(ch for ch in val if ch.isdigit())
        if len(digits) >= 6:
            return digits[-6:]
    return ""


def term_columns(code: str, raw: dict, *, as_of: str) -> dict:
    """`kzz_info` 原始 dict → `cb_terms` 列字典(纯函数, 单测直接喂 dict)。

    字段映射逐条对齐 `marketdata.vendors.tq.kzz_info` 的注释, 不做二次贴现/估算
    (A-06/A-08 同一课: 与客户端数字不一致 = 用户认为数据是假的)。
    """
    return {
        "code": code,
        "as_of": as_of,
        "name": (str(raw.get("KZZName") or "").strip() or None),
        "stock_code": _stock_code_of(raw) or None,
        "conv_price": _float(raw.get("ZGPrice")),
        "force_redeem": _float(raw.get("ForceRedeem")),
        "pure_bond_value": _float(raw.get("RealValue")),
        "rating": (str(raw.get("HSScore") or "").strip() or None),
        # 列表日客户端未稳定给出(ListDate/SGDate 皆可能缺), 缺就 NULL, 不猜
        "list_date": _iso_date(raw.get("ListDate") or raw.get("SGDate")),
        "maturity_date": _iso_date(raw.get("EndDate")),
        "conv_start": _iso_date(raw.get("ZGDate")),
        "put_back_price": _float(raw.get("PutBack")),
        "rest_scope": _float(raw.get("RestScope")),
        "cur_rate": _float(raw.get("CurRate")),
        "source": "tq_kzz_info",
        "captured_at": time.time(),
        "raw": raw or {},
    }


# --------------------------------------------------------------------------- #
# 写: 条款版本 / 当日快照
# --------------------------------------------------------------------------- #
def upsert_terms(
    session: Session,
    code: str,
    raw: dict,
    *,
    as_of: str | None = None,
) -> bool:
    """写入一期条款(`as_of` 版本内原地覆盖)。空条款不落库(不制造"抓到过"的假象)。"""
    if not raw or not isinstance(raw, dict):
        return False
    if not as_of:
        try:
            from src.core.trade_phase import cn_now

            as_of = cn_now().date().isoformat()
        except Exception:  # noqa: BLE001
            as_of = date.today().isoformat()
    values = term_columns(code, raw, as_of=as_of)
    row = session.scalar(
        select(CbTerms).where(CbTerms.code == code, CbTerms.as_of == values["as_of"])
    )
    if row is None:
        session.add(CbTerms(**values))
    else:
        for k, v in values.items():
            setattr(row, k, v)
    session.commit()
    return True


def upsert_daily_rows(
    session: Session,
    rows: Iterable[dict],
    *,
    trade_date: str,
    universe: int | None = None,
    captured_at: float | None = None,
) -> int:
    """写入当日 radar 行(唯一键 (trade_date, code), 同日重跑原地覆盖)。

    只接受可用行: `code` 为空或整行没有债券价的降级行不落库 —— 否则回测集里会
    留下"当天这只债没数据"的假事实。返回写入行数。
    """
    ts = float(captured_at if captured_at is not None else time.time())
    n = 0
    for rank, r in enumerate(rows or [], start=1):
        code = str(r.get("code") or "").strip()
        if not code or r.get("bond_price") is None:
            continue
        values = {
            "trade_date": trade_date,
            "code": code,
            "name": r.get("name"),
            "stock_code": r.get("stock_code") or None,
            "universe_size": int(universe) if universe is not None else None,
            "bond_price": r.get("bond_price"),
            "bond_prev_close": r.get("bond_prev_close"),
            "stock_price": r.get("stock_price"),
            "stock_prev_close": r.get("stock_prev_close"),
            "conv_value": r.get("conv_value"),
            "premium_pct": r.get("premium_pct"),
            "double_low": r.get("double_low"),
            "double_low_rank": r.get("double_low_rank"),
            "force_redeem_dist": r.get("redeem_distance_pct"),
            "pure_bond_premium": r.get("pure_bond_premium_pct"),
            "scenarios": r.get("scenario_keys") or [],
            "rank": r.get("rank") or rank,
            "payload": r,
            "source": "tq",
            "captured_at": ts,
        }
        row = session.scalar(
            select(CbDailySnapshot).where(
                CbDailySnapshot.trade_date == trade_date,
                CbDailySnapshot.code == code,
            )
        )
        if row is None:
            session.add(CbDailySnapshot(**values))
        else:
            for k, v in values.items():
                setattr(row, k, v)
        n += 1
    if n:
        session.commit()
    return n


# --------------------------------------------------------------------------- #
# 读: 条款 / 当日快照
# --------------------------------------------------------------------------- #
def load_terms(
    session: Session,
    codes: Iterable[str],
    *,
    max_age_days: int = TERMS_MAX_AGE_DAYS,
    today: str | None = None,
) -> dict[str, dict]:
    """批量取条款 `{code: raw}`(每只取 `as_of` 最新一行)。

    **不比 max_age_days 就丢行**: 过期条款仍可能是唯一可用数据, 由调用方决定是否
    回源刷新; 这里只按 as_of 降序取最新, 以 `_as_of` 字段回传给调用方判断新鲜度。
    """
    want = [str(c) for c in (codes or []) if c]
    if not want:
        return {}
    # 一次 IN 查询 + Python 去重(取每只 as_of 最新): 60 只转债只打 1 次库,
    # 而不是 60 次 —— 冷启动秒开正是靠这个(方案 §5.3 的 N+1 教训)。
    rows = session.scalars(
        select(CbTerms)
        .where(CbTerms.code.in_(want))
        .order_by(CbTerms.code.asc(), CbTerms.as_of.desc())
    ).all()
    out: dict[str, dict] = {}
    for row in rows:
        code = str(row.code)
        if code in out:
            continue  # 已取到更新的 as_of
        raw = row.raw if isinstance(row.raw, dict) else {}
        # raw 为空(历史库/手写 INSERT)时用结构化列重建 —— 读侧仍需 ZGPrice 等键名
        if not raw:
            raw = {
                "KZZName": row.name,
                "ZGPrice": row.conv_price,
                "ForceRedeem": row.force_redeem,
                "RealValue": row.pure_bond_value,
                "HSScore": row.rating,
                "EndDate": row.maturity_date,
                "ZGDate": row.conv_start,
                "PutBack": row.put_back_price,
                "RestScope": row.rest_scope,
                "CurRate": row.cur_rate,
            }
        raw = dict(raw)
        raw["_as_of"] = str(row.as_of)
        raw["_stock_code"] = row.stock_code or _stock_code_of(raw)
        out[code] = raw
    return out


def terms_are_fresh(raw: dict, *, max_age_days: int = TERMS_MAX_AGE_DAYS) -> bool:
    """条款是否在有效期内(`as_of` 距今 ≤ max_age_days; 解析失败视为不新鲜)。"""
    as_of = str((raw or {}).get("_as_of") or "")
    try:
        d = date.fromisoformat(as_of)
    except ValueError:
        return False
    try:
        from src.core.trade_phase import cn_now

        today = cn_now().date()
    except Exception:  # noqa: BLE001
        today = date.today()
    return (today - d).days <= max(0, int(max_age_days))


def latest_trade_date(session: Session) -> str | None:
    """最近有快照行的交易日(ISO 文本)。"""
    return session.scalar(
        select(CbDailySnapshot.trade_date)
        .order_by(CbDailySnapshot.trade_date.desc())
        .limit(1)
    )


def load_daily_rows(
    session: Session,
    *,
    limit: int = 60,
    trade_date: str | None = None,
) -> dict[str, Any] | None:
    """读最近(或指定)交易日的 radar 行。

    返回 `{trade_date, rows, universe, captured_at, age_s}`; 无行 → None。
    历史日**永不回源** —— TQ 只给当日行情, 回源既拿不到历史, 还会把今天的
    价格污染到历史日上(与 price_distribution 同一口径)。
    """
    day = trade_date or latest_trade_date(session)
    if not day:
        return None
    rows = session.scalars(
        select(CbDailySnapshot)
        .where(CbDailySnapshot.trade_date == day)
        .order_by(CbDailySnapshot.rank.asc(), CbDailySnapshot.code.asc())
        .limit(max(1, int(limit)))
    ).all()
    if not rows:
        return None
    payloads = []
    for r in rows:
        p = r.payload if isinstance(r.payload, dict) else None
        if not p:
            # payload 缺失时用结构化列重建最小可用行(不编造 scenarios)
            p = {
                "code": r.code,
                "name": r.name,
                "stock_code": r.stock_code,
                "bond_price": r.bond_price,
                "bond_prev_close": r.bond_prev_close,
                "stock_price": r.stock_price,
                "stock_prev_close": r.stock_prev_close,
                "conv_price": None,
                "conv_value": r.conv_value,
                "premium_pct": r.premium_pct,
                "double_low": r.double_low,
                "double_low_rank": r.double_low_rank,
                "redeem_distance_pct": r.force_redeem_dist,
                "pure_bond_premium_pct": r.pure_bond_premium,
                "scenarios": [],
                "scenario_keys": r.scenarios or [],
                "missing": ["payload"],
            }
        payloads.append(p)
    newest = max(float(r.captured_at or 0) for r in rows)
    return {
        "trade_date": day,
        "rows": payloads,
        "universe": next((r.universe_size for r in rows if r.universe_size), None),
        "captured_at": newest,
        "age_s": round(max(0.0, time.time() - newest), 1),
    }


def persist_radar(payload: dict, *, session_factory=None) -> dict:
    """把一次成功构建的 radar 响应落库。返回 `{rows, error, trade_date}`。"""
    rows = payload.get("rows") or []
    trade_date = payload.get("data_date")
    if not payload.get("available") or not rows or not trade_date:
        return {"rows": 0, "error": None, "trade_date": trade_date}
    try:
        with (session_factory or SessionLocal)() as session:
            n = upsert_daily_rows(
                session,
                rows,
                trade_date=trade_date,
                universe=payload.get("universe"),
            )
        return {"rows": n, "error": None, "trade_date": trade_date}
    except Exception as e:  # noqa: BLE001 - 落库失败不阻断响应
        logger.warning("转债快照落库失败(%s): %s", trade_date, e)
        return {"rows": 0, "error": str(e) or e.__class__.__name__, "trade_date": trade_date}


def purge_old_snapshots(session: Session, *, keep_days: int = 0, today: str | None = None) -> int:
    """清理超保留期快照(`keep_days=0` 表示永久保留, 直接返回 0)。"""
    if keep_days <= 0:
        return 0
    base = date.fromisoformat(today) if today else date.today()
    cutoff = (base - timedelta(days=int(keep_days))).isoformat()
    from sqlalchemy import delete

    res = session.execute(delete(CbDailySnapshot).where(CbDailySnapshot.trade_date < cutoff))
    session.commit()
    return int(res.rowcount or 0)
