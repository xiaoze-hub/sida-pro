# -*- coding: utf-8 -*-
"""用户自选池专属信号质量报告(2026-10-11 胜率证据层 ②): 纯账本聚合, 零预测。

回答一个问题: **"我自选池里的这几类信号, 实测到底管不管用?"**
按 **user_id 隔离**(多用户铁律) × signal_kind × symbol 聚合决策账本(decision_log)的
T+1/3/5 命中率, 并给出 regime(情绪周期)分桶, 让"该信号只在发酵期有效"这类结论可见。

## 三条纪律(与 decision_log.stats / 全仓诚实口径一致)
1. **样本不足不给数字**: `n < MIN_SAMPLE` → `insufficient=True` 且 `hit_rate=None`,
   页面显示"样本不足", 不拿 3 个样本算 67%。
2. **未回填不是 0**: 只统计 `hit_tN IS NOT NULL` 的行, 缺失不进分母, 不推算。
3. **只陈述实测, 不写建议**: 本模块输出命中率/样本量/分桶, **绝不给"建议买入"** ——
   AI 备证据, 不拍方向。

## 多用户隔离
自选池 = `stocks` 表中 `user_id = <当前用户>` 的标的; 聚合只在**这些标的**的账本行上做。
`user_id` 缺失 → `available=False`(无法隔离即不给报告, 不退化到全局池)。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.core.decision_log import MIN_SAMPLE, _horizon_block, _today_cst

logger = logging.getLogger(__name__)


def _watchlist_symbols(engine: Engine, user_id: str) -> list[str]:
    """该用户自选池的 CN 标的(去重升序)。表缺失/异常 → []。"""
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT DISTINCT symbol FROM stocks "
                    "WHERE user_id = :uid AND market = 'CN' AND symbol IS NOT NULL"
                ),
                {"uid": user_id},
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 —— 表缺失/方言异常 → 无自选池
        logger.debug("自选池读取失败: %r", exc)
        return []
    return sorted({str(r[0]).strip() for r in rows if r[0] and str(r[0]).strip()})


def _since(days: int) -> tuple[str, str]:
    since = (date.fromisoformat(_today_cst()) - timedelta(days=max(7, int(days)))).isoformat()
    return since, since.replace("-", "")


def watchlist_signal_quality(
    engine: Engine,
    *,
    user_id: str,
    days: int = 180,
    min_sample: int = MIN_SAMPLE,
) -> dict[str, Any]:
    """用户自选池信号质量(只读聚合)。

    Returns:
        {
          available: bool,          # false = 无法隔离/无自选/查询失败(带 error)
          user_id, days, min_sample, symbols_count,
          signals: [
            { signal_kind, n_total, horizons:{t1,t3,t5:{n,hit_rate,insufficient,note}},
              by_symbol:[{symbol,n_total,horizons:{...}}],
              regime_buckets:[{regime,label,n_total,horizons:{...}}],
              regime_note }   # 如『该信号只在发酵期有效(样本足够)』
          ],
          note,
        }
    """
    uid = str(user_id or "").strip()
    base: dict[str, Any] = {
        "available": False,
        "user_id": uid,
        "days": int(days),
        "min_sample": int(min_sample),
        "symbols_count": 0,
        "signals": [],
        "note": "",
    }
    if not uid:
        base["error"] = "缺少 user_id —— 无法做多用户隔离, 不给报告(不退化到全局池)"
        base["note"] = base["error"]
        return base

    symbols = _watchlist_symbols(engine, uid)
    base["symbols_count"] = len(symbols)
    if not symbols:
        base["error"] = "自选池为空 —— 先自选标的, 账本才有样本可聚合"
        base["note"] = base["error"]
        return base

    since, since_c = _since(days)
    in_clause = ", ".join(f":s{i}" for i in range(len(symbols)))
    params: dict[str, Any] = {f"s{i}": s for i, s in enumerate(symbols)}
    params.update({"since": since, "since_c": since_c})

    where = (
        f"WHERE symbol IN ({in_clause}) AND "
        "((length(trade_date) = 8 AND trade_date >= :since_c) OR "
        "(length(trade_date) = 10 AND trade_date >= :since))"
    )

    try:
        with engine.connect() as conn:
            kind_sym = conn.execute(
                text(
                    "SELECT signal_kind, symbol, COUNT(*) AS n, "
                    "COUNT(hit_t1) AS n1, COUNT(hit_t3) AS n3, COUNT(hit_t5) AS n5, "
                    "SUM(CASE WHEN hit_t1 = 1 THEN 1 ELSE 0 END) AS w1, "
                    "SUM(CASE WHEN hit_t3 = 1 THEN 1 ELSE 0 END) AS w3, "
                    "SUM(CASE WHEN hit_t5 = 1 THEN 1 ELSE 0 END) AS w5 "
                    f"FROM decision_log {where} GROUP BY signal_kind, symbol"
                ),
                params,
            ).fetchall()
            reg_rows = conn.execute(
                text(
                    "SELECT signal_kind, COALESCE(NULLIF(regime, ''), 'unknown') AS reg, "
                    "COUNT(*) AS n, COUNT(hit_t1) AS n1, COUNT(hit_t3) AS n3, COUNT(hit_t5) AS n5, "
                    "SUM(CASE WHEN hit_t1 = 1 THEN 1 ELSE 0 END) AS w1, "
                    "SUM(CASE WHEN hit_t3 = 1 THEN 1 ELSE 0 END) AS w3, "
                    "SUM(CASE WHEN hit_t5 = 1 THEN 1 ELSE 0 END) AS w5 "
                    f"FROM decision_log {where} GROUP BY signal_kind, reg"
                ),
                params,
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 —— 账本不可用 → 显式失败(不造假)
        base["error"] = f"决策账本聚合失败: {exc}"
        base["note"] = base["error"]
        return base

    # 按 signal_kind 归并 symbol 明细
    by_kind: dict[str, dict[str, Any]] = {}
    for kind, symbol, n, n1, n3, n5, w1, w3, w5 in kind_sym:
        k = by_kind.setdefault(
            str(kind), {"symbols": [], "_n": 0, "_n1": 0, "_n3": 0, "_n5": 0, "_w1": 0, "_w3": 0, "_w5": 0}
        )
        k["symbols"].append(
            {
                "symbol": str(symbol),
                "n_total": int(n or 0),
                "horizons": _horizon_block(n, n1, n3, n5, w1, w3, w5, min_sample),
            }
        )
        k["_n"] += int(n or 0)
        k["_n1"] += int(n1 or 0)
        k["_n3"] += int(n3 or 0)
        k["_n5"] += int(n5 or 0)
        k["_w1"] += int(w1 or 0)
        k["_w3"] += int(w3 or 0)
        k["_w5"] += int(w5 or 0)

    # regime 分桶
    reg_by_kind: dict[str, list[dict[str, Any]]] = {}
    for kind, reg, n, n1, n3, n5, w1, w3, w5 in reg_rows:
        reg_by_kind.setdefault(str(kind), []).append(
            {
                "regime": str(reg),
                "label": _regime_label(str(reg)),
                "n_total": int(n or 0),
                "horizons": _horizon_block(n, n1, n3, n5, w1, w3, w5, min_sample),
            }
        )

    out_signals: list[dict[str, Any]] = []
    for kind, agg in sorted(by_kind.items(), key=lambda kv: -kv[1]["_n"]):
        buckets = sorted(
            reg_by_kind.get(kind, []),
            key=lambda b: (b["horizons"]["t1"]["insufficient"], -b["n_total"]),
        )
        out_signals.append(
            {
                "signal_kind": kind,
                "n_total": agg["_n"],
                "symbols_count": len(agg["symbols"]),
                "horizons": _horizon_block(
                    agg["_n"], agg["_n1"], agg["_n3"], agg["_n5"], agg["_w1"], agg["_w3"], agg["_w5"], min_sample
                ),
                "by_symbol": sorted(agg["symbols"], key=lambda s: -s["n_total"]),
                "regime_buckets": buckets,
                "regime_note": _regime_note(buckets),
            }
        )

    base["available"] = True
    base["signals"] = out_signals
    base["note"] = (
        "纯账本聚合(零预测): 命中 = T+n 收益 > 0(平盘记未命中); 只统计已回填行, 缺失不进分母。"
        f" 样本 < {int(min_sample)} 一律显示『样本不足』, 不给数字。只陈述实测, 不构成建议。"
    )
    return base


def _regime_label(regime: str) -> str:
    """规范 key → 中文名(None/未知原样透出, 不归类编造)。"""
    try:
        from src.core.market_regime import REGIME_LABELS

        return REGIME_LABELS.get(regime, regime)
    except Exception:  # noqa: BLE001
        return regime


def _regime_note(buckets: list[dict[str, Any]]) -> str:
    """给 regime 分桶一句人话标注(如『该信号只在发酵期有效』); 无定论则空串。"""
    enough = [b for b in buckets if not b["horizons"]["t1"]["insufficient"]]
    if len(enough) == 1 and len(buckets) > 1:
        return f"该信号仅在『{enough[0]['label']}』期样本足够(其余状态样本不足, 暂无法比较)"
    if len(enough) >= 2:
        best = max(enough, key=lambda b: (b["horizons"]["t1"]["hit_rate"] or 0))
        return f"多状态可比: 『{best['label']}』期 T+1 命中最高({(best['horizons']['t1']['hit_rate'] or 0) * 100:.1f}%)"
    return ""
