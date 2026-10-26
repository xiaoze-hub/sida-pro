"""TQ 条件选股信号 API(2026-09-25)。

暴露 `tq_formula_signal_daily`: 每个条件选股公式的**全市场当日触发家数** + 近 N 日基线。
`GET /batch`(2026-10-02)为**按需批量执行**: 给定公式集 × (全市场 | 指定池) 直接返回命中
清单, 不落库(引擎见 `src/collectors/tq_formula_batch.py`)。

口径 / 来源: 通达信客户端 TQ 网关条件选股(`formula_process_mul_xg`, 108 个公式),
采集见 `src/collectors/tq_formula_signals.py`, 调度见 `src/core/tq_formula_signal_scheduler.py`。
对照: 问财限频 250ms; 东财/同花顺免费层没有"某技术条件当日触发家数"的口径。

诚实性: `complete=False` 表示该日扫描有分片失败 ⇒ `hit_count` **不是**全市场命中数,
响应里原样带出该标记, 前端必须显示为不完整(不得当"全市场家数"用)。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from src.web.cache.biz_cache import biz_cache
from src.web.database import SessionLocal

logger = logging.getLogger(__name__)

router = APIRouter()

_CACHE_KEY = "mkt:formula-signals"
_CACHE_TTL = 300  # 日频数据, 5 分钟缓存足够(采集是盘后一次性写入)


@router.get("")
def formula_signals(days: int = Query(20, ge=1, le=120)) -> dict:
    """最新一日的各公式信号家数 + 近 N 日基线(按命中数降序)。"""
    key = f"{_CACHE_KEY}:{days}"
    cached = biz_cache.get_json(key)
    if cached is not None:
        return cached

    from src.collectors.tq_formula_signals import latest_with_baseline

    db = SessionLocal()
    try:
        out = latest_with_baseline(db, days=days)
    except Exception as e:  # noqa: BLE001 — 表未建(迁移未跑)时显式降级, 不编造
        logger.warning("条件选股信号读取失败: %s", e)
        return {"trade_date": "", "items": [], "degraded": True,
                "note": f"读取失败: {str(e)[:120]}"}
    finally:
        db.close()

    out["count"] = len(out.get("items") or [])
    if not out.get("items"):
        out["degraded"] = True
        out["note"] = ("暂无采集数据(调度器每交易日 15:50 写入); "
                       "非「今日无信号」")
    biz_cache.set_json(key, out, ttl=_CACHE_TTL)
    return out


@router.get("/batch")
def formula_signals_batch(
    formulas: str = Query(
        ...,
        description="公式集, 逗号分隔; 每个可带参数写成 `代码:参数`(如 `UPN:3`), 不带参数则为空参",
    ),
    date: str = Query("", description="交易日(YYYYMMDD 或 ISO); 缺省=今天; 非交易日不扫并显式提示"),
    codes: str = Query("", description="可选标的池(逗号分隔); 缺省=全市场"),
    formula_arg: str = Query("", description="统一默认参数(公式未单独带 `:arg` 时生效)"),
    chunk: int = Query(0, ge=0, le=500, description="分片大小; 0=用默认 TQ_SCAN_CHUNK(500)"),
    limit: int = Query(500, ge=1, le=5000, description="每公式返回的命中数上限(超出只截断展示, 计数不受影响)"),
) -> dict:
    """**按需**批量执行: 公式集 × (全市场 | 指定池) → 每公式命中清单, 不落库。

    与 `/api/formula-signals`(读定时采集落库的家数+基线)互补: 这里直接对**给定公式集**
    全市场/指定池扫一遍, 返回命中标的。

    诚实性(硬约束, 与定时采集同口径):
      · 分片失败**不当作 0 命中** —— 该公式 `complete=False` 且 `missing_chunks` 列出缺哪片;
      · 非交易日**不扫**, 返回 `skipped=true` + `note`;
      · `date_has_data=false` 表示该日无数据行(请求日≠有效数据日), 与「当日无票触发」不是一回事。

    ⚠️ 全市场 × N 公式每公式约 24s, 接口为**同步长请求**, 公式数上限见 `MAX_BATCH_FORMULAS`。
    """
    from src.collectors.tq_formula_batch import MAX_BATCH_FORMULAS, _normalize_formulas, run_formula_batch

    raw_specs = []
    for token in (formulas or "").split(","):
        token = token.strip()
        if not token:
            continue
        code, _, arg = token.partition(":")
        raw_specs.append({"code": code.strip(), "arg": arg.strip() or formula_arg})
    specs = _normalize_formulas(raw_specs)
    if not specs:
        raise HTTPException(400, "formulas 为空: 需给出至少一个公式代码")
    if len(specs) > MAX_BATCH_FORMULAS:
        raise HTTPException(400, f"公式数 {len(specs)} 超过上限 {MAX_BATCH_FORMULAS}")

    pool = [c.strip() for c in (codes or "").split(",") if c.strip()] or None
    out = run_formula_batch(
        specs, trade_date=date, codes=pool, chunk=(chunk or None),
    )
    # 展示上限: 只截断 hits 列表, **不动 hit_count**(计数是全池真实命中数)。
    for f in out.get("formulas") or []:
        if len(f.get("hits") or []) > limit:
            f["hits_truncated"] = True
            f["hits_total"] = f["hit_count"]
            f["hits"] = f["hits"][:limit]
    return out


@router.get("/{formula_code}")
def formula_signal_series(
    formula_code: str,
    days: int = Query(30, ge=1, le=250),
    formula_arg: str = Query("", description="公式参数(UPN/DOWNN 这类需要)"),
) -> dict:
    """单公式的日序列(家数 + 扫描数 + 完整标记) + 最近一日的命中标的清单。"""
    from sqlalchemy import text

    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                """
                SELECT trade_date, hit_count, scanned_count, chunks_failed, complete,
                       truncated, hits_json, formula_name
                  FROM tq_formula_signal_daily
                 WHERE formula_code = :code AND formula_arg = :arg
                 ORDER BY trade_date DESC
                 LIMIT :lim
                """
            ),
            {"code": formula_code, "arg": formula_arg, "lim": int(days)},
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"条件选股信号表不可用: {str(e)[:120]}") from e
    finally:
        db.close()

    if not rows:
        raise HTTPException(404, f"无 {formula_code}(arg={formula_arg!r}) 的采集数据")

    import json

    head = dict(rows[0]._mapping)
    series = [
        {
            "trade_date": r._mapping["trade_date"],
            "hit_count": r._mapping["hit_count"],
            "scanned_count": r._mapping["scanned_count"],
            "complete": bool(r._mapping["complete"]),
            "chunks_failed": r._mapping["chunks_failed"],
        }
        for r in rows
    ]
    try:
        latest_hits = json.loads(head.get("hits_json") or "[]")
    except Exception:  # noqa: BLE001
        latest_hits = []
    return {
        "formula_code": formula_code,
        "formula_arg": formula_arg,
        "formula_name": head.get("formula_name") or "",
        "trade_date": head.get("trade_date"),
        "hit_count": head.get("hit_count"),
        "scanned_count": head.get("scanned_count"),
        "complete": bool(head.get("complete")),
        "truncated": bool(head.get("truncated")),
        "latest_hits": latest_hits,
        "series": list(reversed(series)),
    }
