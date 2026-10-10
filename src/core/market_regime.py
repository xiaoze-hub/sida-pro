# -*- coding: utf-8 -*-
"""情绪周期条件化门槛(2026-10-10 决策提胜率 A): 把"当前情绪周期状态"引入决策合成。

## 为什么
三指标共振的命中率此前是**全状态混算的平均数**(决策账本 resonance3: T+1 53.85%)。
但同一套"动手"门槛在冰点/退潮 与 发酵/修复 期的含义完全不同 —— 混算会把
"退潮期该躲的"和"发酵期该打的"平均成一个没有操作意义的数字。本模块把
**当前情绪周期**读出来, 对『动手』门槛做**显式可解释**的条件化, 并把 regime
落到决策账本, 让命中率可按状态分桶统计(只加聚合维度, 不建回测 UI)。

## 口径来源(复用仓库现有, 不新造)
1. 主源 `signal_summary_daily.blocks.sentiment.cycle` —— 由
   `src.core.sentiment_cycle.classify_sentiment_cycle` 判定(冰点/修复/发酵/高潮/退潮),
   盘后 cron 落库(`src.core.signal_summary`); 与对话/报告同口径。
2. 兜底 `market_phase_daily.phase` —— `src.core.market_phase` 的 7 阶段(EMA+2日确认),
   经 `src.core.mood_cycle.PHASE_ALLOWANCE` 的 key 空间; 较粗, 仅当主源缺失时用。
3. 两者都缺 → `regime="unknown"`(**显式缺数据**, 按标准口径放行, 不做条件化)。

## 诚实纪律
- 读不到 → `unknown`, 绝不猜某个状态;
- 条件化只改『动手』档(收紧=收窄允许动手的状态表行; 放宽=纳入拐点行), 不动
  『看看/别碰』语义, 且每次改动都写 `regime_note`(可解释, 不黑箱)。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# 规范 regime key(与 sentiment_cycle 五态对齐; unknown = 数据不足)
REGIMES = ("ice", "repair", "ferment", "climax", "ebb", "unknown")

# 情绪周期中文名 → key(主源: classify_sentiment_cycle 输出)
CYCLE_TO_REGIME: dict[str, str] = {
    "冰点": "ice",
    "修复": "repair",
    "发酵": "ferment",
    "高潮": "climax",
    "退潮": "ebb",
}

# market_phase 7 阶段 → key(兜底; ignite/rally 归发酵, accumulating 归修复)
PHASE_TO_REGIME: dict[str, str] = {
    "ice": "ice",
    "ignite": "ferment",
    "rally": "ferment",
    "climax": "climax",
    "ebb": "ebb",
    "repair": "repair",
    "accumulating": "repair",
}

REGIME_LABELS: dict[str, str] = {
    "ice": "冰点",
    "repair": "修复",
    "ferment": "发酵",
    "climax": "高潮",
    "ebb": "退潮",
    "unknown": "数据不足",
}

#: 状态表标准口径下『动手』对应的行(evaluate_state: 向好 = 行1 首次共振 / 行3 平稳)
BASE_TOUCH_ROWS: tuple[int, ...] = (1, 3)

#: 条件化政策 —— 每态显式给出允许动手的状态表行 + 中文说明(阈值差异必须可解释)
REGIME_POLICY: dict[str, dict[str, Any]] = {
    "ice": {
        "policy": "tighten",
        "allow_touch_rows": (1,),
        "note": "冰点期: 空仓防守, 主力未启动, 仅保留最强『三指标首次共振』给动手(行1), 平稳定向好(行3)收敛为看看",
    },
    "ebb": {
        "policy": "tighten",
        "allow_touch_rows": (1,),
        "note": "退潮期: 避雷优先, 收紧动手门槛, 仅最强共振(行1)保留动手, 其余收敛为看看",
    },
    "climax": {
        "policy": "tighten",
        "allow_touch_rows": (1,),
        "note": "高潮期: 只卖不买倾向, 收紧动手门槛, 仅最强共振(行1)保留动手",
    },
    "repair": {
        "policy": "loosen",
        "allow_touch_rows": (1, 2, 3),
        "note": "冰点转暖(修复期): 放宽门槛, 拐点(再次共振 行2)也放开为动手",
    },
    "ferment": {
        "policy": "normal",
        "allow_touch_rows": (1, 3),
        "note": "发酵期: 按状态表标准口径, 不做条件化",
    },
    "unknown": {
        "policy": "normal",
        "allow_touch_rows": (1, 3),
        "note": "情绪周期无数据: 按状态表标准口径, 不做条件化(缺数据显式, 不猜状态)",
    },
}

# 进程内短缓存(情绪态按日刷新, 60s 足够; 避免决策热路径每标的都查库)
_MEMO_TTL_S = 60.0
_memo_lock = threading.Lock()
_memo: dict[str, Any] = {"ts": 0.0, "value": None}


def normalize_regime(value: str | None) -> str:
    """任意输入(中文周期名 / phase key / 已规范 key) → 规范 key; 未知 → 'unknown'。"""
    if value is None:
        return "unknown"
    s = str(value).strip()
    if not s:
        return "unknown"
    if s in REGIMES:
        return s
    if s in CYCLE_TO_REGIME:
        return CYCLE_TO_REGIME[s]
    if s in PHASE_TO_REGIME:
        return PHASE_TO_REGIME[s]
    return "unknown"


def regime_policy(regime: str | None) -> dict[str, Any]:
    """取某态的政策(未知 → unknown 政策)。返回带 label 的副本。"""
    key = normalize_regime(regime)
    pol = dict(REGIME_POLICY.get(key, REGIME_POLICY["unknown"]))
    pol["regime"] = key
    pol["label"] = REGIME_LABELS.get(key, key)
    return pol


def condition_verdict(
    verdict: str, row: int | None, regime: str | None
) -> tuple[str, bool, str | None]:
    """对『动手』档做情绪周期条件化(纯函数)。

    Returns:
        (verdict_after, adjusted, note)
        - tighten: 『动手』但状态表行不在允许集 → 收敛为『看看』;
        - loosen : 状态表行在允许集但基线不给『动手』(如拐点 行2) → 放开为『动手』;
        - normal / unknown: 与基线一致(向后兼容);
        - 只改『动手』档, 不动『别碰』。
    """
    pol = regime_policy(regime)
    allowed = set(pol["allow_touch_rows"])
    label = pol["label"]
    r = row if isinstance(row, int) else None

    if verdict == "动手" and r is not None and r not in allowed:
        return (
            "看看",
            True,
            f"情绪周期条件化({label}/{pol['policy']}): 状态表行{r} 不在本态允许动手行 "
            f"{sorted(allowed)}, 『动手』收敛为『看看』",
        )
    if verdict != "动手" and r is not None and r in allowed and r not in BASE_TOUCH_ROWS:
        return (
            "动手",
            True,
            f"情绪周期条件化({label}/{pol['policy']}): 状态表行{r} 在本态放宽行内, "
            f"放开为『动手』",
        )
    return verdict, False, None


def _read_signal_summary_cycle(db) -> tuple[str | None, str | None]:
    """读最新 signal_summary_daily 的情绪周期块 → (cycle, confidence)。缺 → (None, None)。"""
    from sqlalchemy import desc

    from src.db.models import SignalSummaryDaily

    row = (
        db.query(SignalSummaryDaily)
        .order_by(desc(SignalSummaryDaily.snapshot_date), desc(SignalSummaryDaily.id))
        .first()
    )
    if row is None:
        return None, None
    blocks = row.blocks if isinstance(row.blocks, dict) else None
    if not blocks:
        return None, None
    senti = blocks.get("sentiment") or {}
    if not isinstance(senti, dict):
        return None, None
    cycle = senti.get("cycle")
    if not cycle:
        return None, None
    return str(cycle), (str(senti.get("confidence")) if senti.get("confidence") else None)


def _read_market_phase(db) -> str | None:
    """读最新 market_phase_daily.phase(兜底源)。缺 → None。"""
    from sqlalchemy import desc

    from src.db.models import MarketPhaseDaily

    row = db.query(MarketPhaseDaily).order_by(desc(MarketPhaseDaily.date)).first()
    if row is None:
        return None
    return str(row.phase) if row.phase else None


def _resolve(db=None) -> dict[str, Any]:
    """读当前情绪周期态(不走网络)。永远返回 dict, 失败 → available=False + reason。"""
    from src.db.session import SessionLocal

    owns = db is None
    session = db
    try:
        if session is None:
            session = SessionLocal()
        try:
            cycle, confidence = _read_signal_summary_cycle(session)
            if cycle:
                key = normalize_regime(cycle)
                if key != "unknown":
                    return {
                        "available": True,
                        "regime": key,
                        "label": REGIME_LABELS.get(key, key),
                        "cycle": cycle,
                        "confidence": confidence,
                        "source": "signal_summary",
                    }
            phase = _read_market_phase(session)
            if phase:
                key = normalize_regime(phase)
                if key != "unknown":
                    return {
                        "available": True,
                        "regime": key,
                        "label": REGIME_LABELS.get(key, key),
                        "cycle": phase,
                        "confidence": None,
                        "source": "market_phase",
                    }
            return {
                "available": False,
                "regime": "unknown",
                "label": REGIME_LABELS["unknown"],
                "cycle": None,
                "confidence": None,
                "source": "none",
                "reason": "无情绪周期快照(signal_summary_daily / market_phase_daily 均无数据)",
            }
        finally:
            if owns and session is not None:
                session.close()
    except Exception as e:  # noqa: BLE001 —— 读失败也是显式缺数据, 不猜
        logger.debug("current_regime 读取失败: %r", e)
        return {
            "available": False,
            "regime": "unknown",
            "label": REGIME_LABELS["unknown"],
            "cycle": None,
            "confidence": None,
            "source": "error",
            "reason": f"读取失败: {e}",
        }


def current_regime(db=None, *, use_memo: bool = True) -> dict[str, Any]:
    """当前情绪周期态(带 60s 进程内缓存)。

    Returns: {available, regime, label, cycle, confidence, source[, reason]}
    - available=True: 读到明确状态;
    - available=False + regime='unknown': 显式缺数据(caller 按标准口径放行, 不做条件化)。
    """
    if not use_memo:
        return _resolve(db)
    now = time.monotonic()
    with _memo_lock:
        cached = _memo.get("value")
        if cached is not None and (now - float(_memo.get("ts") or 0.0)) < _MEMO_TTL_S:
            return dict(cached)
    out = _resolve(db)
    with _memo_lock:
        _memo["ts"] = now
        _memo["value"] = dict(out)
    return dict(out)


def clear_regime_memo() -> None:
    """清进程内缓存(测试/运维用)。"""
    with _memo_lock:
        _memo["ts"] = 0.0
        _memo["value"] = None


def regime_key(regime: str | None) -> str:
    """规范 key 便捷入口(供账本写入/分桶)。"""
    return normalize_regime(regime)
