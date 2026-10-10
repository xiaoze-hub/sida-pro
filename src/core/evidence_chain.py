"""AI 建议证据化(2026-10-10): 结论必带 证据链 + 失效条件 + 置信度账本校准 + 历史相似情形。

## 为什么
用户铁律: AI 不替用户拍方向, 但要把证据备到最好。任何 AI/规则结论出口都必须让用户看得见
**凭什么**(触发条件)、**在什么时点**(as_of, 非今日显式标注)、**什么时候作废**(失效条件, 反例
检查防单边叙事)、**置信度是怎么来的**(挂钩决策账本实测命中率, 缺样本显式「未校准」)、
**历史上像不像**(从 decision_log 聚合的相似情形命中统计)。

## 五条纪律(与全仓诚实口径一致, 幻觉敏感)
1. **证据链确定性**: 触发条件(triggers)由调用方从**规则/数值**拼出, 不由 LLM 编造展示;
   LLM 只可补充失效条件文本, 缺失时回确定性默认 —— 绝不空。
2. **时点诚实**: `as_of` 缺失 → 显式「时点缺失」, **不默认成今日**; 非今日显式标注基准日。
3. **失效条件必填**: `build_evidence_chain` 保证 `invalidation` 永不为空(缺失回默认并标记
   `invalidation_defaulted=True`)—— 反例检查, 防单边叙事。
4. **置信度只读校准**: 只**只读消费** `decision_log.stats`(`src/core/decision_log.py`)的实测
   命中率; 样本不足(`insufficient`/空)一律 `calibrated=False` + `note="未校准..."`, **不编造**。
5. **相似情形样本不足不给数字**: `n < min_sample` 时 `insufficient=True` 且不给百分比。

本模块纯函数 + 一个**尽力而为**的只读 stats 加载器(`load_stats`, 失败回空壳绝不抛)。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")

#: 缺省校准视界(与 decision_log.HORIZONS 对齐; 共振类建议以 T+1 为主口径)
DEFAULT_HORIZON = "t1"

#: 相似情形/校准的最小样本量兜底(与 decision_log.MIN_SAMPLE 同值; 只读, 不写库)
DEFAULT_MIN_SAMPLE = 30

#: 失效条件通用兜底(确定性, 非编造) —— 任一触发条件反转即判断作废
DEFAULT_INVALIDATION = "任一触发条件反转(指标转弱/资金转向)则此判断作废"


# ────────────────────────────────────────────────────────────────────────────
# 证据链
# ────────────────────────────────────────────────────────────────────────────
def _today_cst_compact() -> str:
    return datetime.now(_CST).strftime("%Y%m%d")


def _norm_day(day: Any) -> str | None:
    """日期 → 紧凑 `YYYYMMDD`; 无法识别返回 None(不猜)。支持 '20260911'/'2026-09-11'。"""
    s = str(day or "").strip()
    if not s:
        return None
    digits = s.replace("-", "").replace("/", "")
    if len(digits) == 8 and digits.isdigit():
        return digits
    return None


def _clean_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for v in values:
        s = str(v).strip()
        if s:
            out.append(s)
    return out


def build_evidence_chain(
    *,
    triggers: Any,
    as_of: Any = None,
    today: Any = None,
    invalidation: Any = None,
    default_invalidation: Any = None,
) -> dict[str, Any]:
    """结构化证据链: 触发条件清单 + 数据时点(as_of) + 失效条件(必填)。

    纯函数, 无 IO。**失效条件永不为空**: LLM/调用方未给 → 回 `default_invalidation`
    (或模块通用兜底)并标记 `invalidation_defaulted=True`。

    Args:
        triggers: 触发条件(哪几个指标/条件成立), str 或 list。
        as_of: 数据时点(交易基准日, 支持 'YYYYMMDD'/'YYYY-MM-DD')。
        today: 参照今日(缺省取 CST 今日; 便于单测注入)。
        invalidation: 失效条件(LLM/调用方给定)。
        default_invalidation: 缺失时的领域化兜底(仍空则用模块通用兜底)。

    Returns:
        {triggers, as_of, as_of_is_today, as_of_note, invalidation, invalidation_defaulted}
    """
    trig = _clean_list(triggers) or ["无可用触发条件(数据缺失)"]

    inval = _clean_list(invalidation)
    defaulted = False
    if not inval:
        inval = _clean_list(default_invalidation) or [DEFAULT_INVALIDATION]
        defaulted = True

    day = _norm_day(as_of)
    ref = _norm_day(today) if today is not None else _today_cst_compact()
    if day is None:
        as_of_note = "数据时点缺失(无法确认是否为今日, 不作实时解读)"
        is_today = False
    elif ref is not None and day == ref:
        as_of_note = f"数据时点: {day}(今日)"
        is_today = True
    else:
        as_of_note = f"数据时点: {day}(非今日, 基准日滞后)"
        is_today = False

    return {
        "triggers": trig,
        "as_of": day,
        "as_of_is_today": is_today,
        "as_of_note": as_of_note,
        "invalidation": inval,
        "invalidation_defaulted": defaulted,
    }


# ────────────────────────────────────────────────────────────────────────────
# 置信度账本校准 + 历史相似情形(只读消费 decision_log.stats 输出)
# ────────────────────────────────────────────────────────────────────────────
def _find_row(stats: dict | None, signal_kind: str | None) -> dict | None:
    if not signal_kind or not isinstance(stats, dict):
        return None
    for r in stats.get("rows") or []:
        if isinstance(r, dict) and r.get("signal_kind") == signal_kind:
            return r
    return None


def _horizon_of(row: dict | None, horizon: str) -> dict:
    if not isinstance(row, dict):
        return {}
    h = (row.get("horizons") or {}).get(horizon)
    return h if isinstance(h, dict) else {}


def _min_sample(stats: dict | None, override: int | None) -> int:
    if override is not None:
        return int(override)
    if isinstance(stats, dict) and stats.get("min_sample") is not None:
        try:
            return int(stats["min_sample"])
        except (TypeError, ValueError):
            pass
    return DEFAULT_MIN_SAMPLE


def calibrate_confidence(
    raw: Any,
    signal_kind: str | None,
    stats: dict | None,
    *,
    horizon: str = DEFAULT_HORIZON,
    min_sample: int | None = None,
) -> dict[str, Any]:
    """原始置信度 → 挂钩该信号**实测命中率**上限(只读, 缺样本显式「未校准」)。

    - 该信号当前视界样本充足(`insufficient=False` 且 `hit_rate` 非空) → `calibrated=True`,
      `cap=hit_rate`, `value=min(raw, cap)`(raw 缺则取 cap);
    - 样本不足 / 无该信号 / 表不可用 → `calibrated=False`, `value=None`, `note="未校准..."`。

    **绝不编造校准数字**: 拿不到实测命中率就不给 `value`。
    """
    ms = _min_sample(stats, min_sample)
    row = _find_row(stats, signal_kind)
    if not signal_kind:
        return {
            "calibrated": False,
            "value": None,
            "cap": None,
            "raw": raw,
            "signal_kind": None,
            "horizon": horizon,
            "n": 0,
            "hit_rate": None,
            "note": "未校准(该类判断无决策账本信号口径, 不给校准数)",
        }
    h = _horizon_of(row, horizon)
    try:
        n = int(h.get("n") or 0)
    except (TypeError, ValueError):
        n = 0
    hit_rate = h.get("hit_rate")
    insufficient = bool(h.get("insufficient", True)) or hit_rate is None or n <= 0

    if row is None or insufficient:
        return {
            "calibrated": False,
            "value": None,
            "cap": None,
            "raw": raw,
            "signal_kind": signal_kind,
            "horizon": horizon,
            "n": n,
            "hit_rate": None,
            "note": f"未校准(样本不足: n={n} < {ms}, 不给校准置信度)",
        }

    cap = float(hit_rate)
    value = cap
    if raw is not None:
        try:
            value = min(max(0.0, min(1.0, float(raw))), cap)
        except (TypeError, ValueError):
            value = cap
    return {
        "calibrated": True,
        "value": round(value, 4),
        "cap": round(cap, 4),
        "raw": raw,
        "signal_kind": signal_kind,
        "horizon": horizon,
        "n": n,
        "hit_rate": round(cap, 4),
        "note": f"置信度上限=该类({horizon.upper()})实测命中率 {round(cap, 4)}",
    }


def historical_similarity(
    signal_kind: str | None,
    stats: dict | None,
    *,
    horizon: str = DEFAULT_HORIZON,
    min_sample: int | None = None,
) -> dict[str, Any]:
    """历史相似情形统计: 「历史上 N 次相似情形, M 次后续上涨」。

    从 `decision_log` 聚合反推(命中率 × 已回填样本 = 上涨次数)。`n < min_sample` →
    `insufficient=True` 且**不给百分比/上涨数**(避免小样本误导)。

    纯函数, 只读 stats 输出(不做 IO, 不写库)。
    """
    ms = _min_sample(stats, min_sample)
    row = _find_row(stats, signal_kind)
    h = _horizon_of(row, horizon)
    try:
        n = int(h.get("n") or 0)
    except (TypeError, ValueError):
        n = 0
    hit_rate = h.get("hit_rate")

    if row is None or h.get("insufficient") or hit_rate is None or n < ms:
        return {
            "signal_kind": signal_kind,
            "horizon": horizon,
            "n": n,
            "up": None,
            "insufficient": True,
            "sentence": f"历史相似情形样本不足(N={n} < {ms}), 不给百分比",
        }
    up = int(round(float(hit_rate) * n))
    return {
        "signal_kind": signal_kind,
        "horizon": horizon,
        "n": n,
        "up": up,
        "insufficient": False,
        "sentence": f"历史上 {n} 次相似情形, {up} 次后续上涨",
    }


# ────────────────────────────────────────────────────────────────────────────
# 只读 stats 加载器(尽力而为, 绝不抛)
# ────────────────────────────────────────────────────────────────────────────
def load_stats(*, days: int = 180, min_sample: int = DEFAULT_MIN_SAMPLE, engine: Any = None) -> dict[str, Any]:
    """只读拉取决策账本 stats(`decision_log.stats`)。

    只读消费 —— **不写库、不改 decision_log**。取引擎/表/查询失败一律回**空壳**
    (`rows=[]`) 并把原因写进 `note`: 缺数据宁标「未校准」, 绝不抛、绝不编造。
    """
    try:
        from src.core.decision_log import stats as _stats
        from src.db.session import get_read_engine

        eng = engine if engine is not None else get_read_engine()
        out = _stats(eng, days=days, min_sample=min_sample)
        if isinstance(out, dict):
            return out
    except Exception as exc:  # noqa: BLE001 —— 账本不可用只降级, 不阻断建议产出
        logger.debug("证据化: 决策账本 stats 不可用(降级未校准): %r", exc)
    return {
        "since": None,
        "min_sample": int(min_sample),
        "rows": [],
        "note": "决策账本不可用(stats 读取失败), 置信度未校准、相似情形不可得。",
    }


# ────────────────────────────────────────────────────────────────────────────
# 便捷装配: 建议证据包(证据链 + 校准 + 相似情形)
# ────────────────────────────────────────────────────────────────────────────
def build_suggestion_evidence(
    *,
    triggers: Any,
    as_of: Any = None,
    invalidation: Any = None,
    default_invalidation: Any = None,
    signal_kind: str | None = None,
    raw_confidence: Any = None,
    stats: dict | None = None,
    horizon: str = DEFAULT_HORIZON,
    today: Any = None,
) -> dict[str, Any]:
    """一次装配证据链 + 置信度校准 + 历史相似情形(供各 AI/规则结论出口复用)。

    `signal_kind` 缺省(该类信号无决策账本口径) → 校准项显式 `未校准`、相似情形为 `None`
    (无口径即不给, 不借别的信号冒充)。
    """
    evidence = build_evidence_chain(
        triggers=triggers,
        as_of=as_of,
        today=today,
        invalidation=invalidation,
        default_invalidation=default_invalidation,
    )
    calibration = calibrate_confidence(raw_confidence, signal_kind, stats, horizon=horizon)
    similar: dict[str, Any] | None = (
        historical_similarity(signal_kind, stats, horizon=horizon) if signal_kind else None
    )
    return {
        "evidence": evidence,
        "confidence_calibration": calibration,
        "similar": similar,
    }


def build_decision_evidence(decision_out: dict, *, signal_kind: str = "resonance3", stats: dict | None = None) -> dict:
    """决策合成(动手/看看/别碰)的证据化装配。纯函数(仅 `parts`/`phase` 等确定性字段), 无 IO。

    - 触发条件: 决策 parts(趋势/活跃度/资金)中**已有值**的项(确定性, 不编);
    - as_of: 决策的 `computed_at`(计算时刻);
    - 失效条件: 由 phase 拼出(向好→转弱即作废; 走坏→转强即解除; 分歧→任一维反转);
    - 相似情形: 从决策账本取 `signal_kind`(默认 resonance3)聚合。
    """
    out = decision_out if isinstance(decision_out, dict) else {}
    parts_raw = out.get("parts")
    parts: dict = parts_raw if isinstance(parts_raw, dict) else {}
    trig: list[str] = []
    trend = parts.get("trend")
    if trend and trend != "无数据":
        trig.append(f"趋势 {trend}")
    act = parts.get("activity")
    if act is not None:
        try:
            trig.append(f"活跃度 {float(act):.2f}")
        except (TypeError, ValueError):
            pass
    fund = parts.get("fund_net")
    if fund is not None:
        try:
            f = float(fund)
            trig.append(f"主力净{'流入' if f > 0 else '流出'} {abs(f) / 1e8:.2f}亿")
        except (TypeError, ValueError):
            pass
    if not trig:
        trig = ["信号不全(关键维缺数据)"]

    phase = out.get("phase")
    if phase == "向好":
        default_inval = ["若趋势转弱 / 活跃度跌破强势线 / 资金转净流出, 则『动手』作废"]
    elif phase == "走坏":
        default_inval = ["若趋势转强 / 活跃度回到强势线 / 资金转净流入, 则『别碰』解除"]
    else:
        default_inval = ["趋势/活跃度/资金任一维反转, 则此判断作废(分歧态无单边结论)"]

    return {
        "evidence": build_evidence_chain(
            triggers=trig,
            as_of=out.get("computed_at"),
            default_invalidation=default_inval,
        ),
        "similar": historical_similarity(signal_kind, stats) if signal_kind else None,
    }
