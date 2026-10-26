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

import json
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


def temperature_policy(regime: str | None, thermo: dict[str, Any] | None = None) -> dict[str, Any]:
    """在情绪周期政策之上叠加**情绪温度计**条件化(纯函数)。

    温度计(`src.core.sentiment_thermometer`)给出当日『封板成功率 / 炸板比』; 本轮把它接入
    『动手』门槛:
      - 封板成功率 < `seal_rate_tighten`(默认 0.50) → **收紧**(仅保留最强行1);
      - 炸板比     ≥ `blow_rate_tighten`(默认 0.45) → **收紧**(分歧加剧, 承接弱);
      - 封板成功率 ≥ `seal_rate_loosen`(默认 0.70) → **放宽**(纳入拐点行2)。
    阈值一律走 `src.core.thresholds`(禁硬编码)。缺数据/可疑一律不条件化(按周期态放行)。

    Returns: 在 `regime_policy` 基础上追加
      - `allow_touch_rows`: 温度修正后的允许动手行;
      - `policy`: 收紧/放宽后的政策(温度优先于周期态同类政策);
      - `note`: 可解释说明(**写明依据**: 如『封板成功率 61%<阈值 50% → 收紧』);
      - `thermo_applied` / `thermo_suspicious` / `temperature`(透传, 供审计)。
    """
    pol = regime_policy(regime)
    pol["thermo_applied"] = False
    pol["thermo_suspicious"] = bool(thermo.get("suspicious")) if thermo else False
    pol["thermo_stale"] = bool(thermo.get("stale")) if thermo else False
    pol["temperature"] = None
    if not thermo or not thermo.get("available"):
        return pol

    # 数据停在过去(2026-10-11 断链修复): **不拿旧值冒充当日**, 不据此条件化, 显式标注。
    if thermo.get("stale"):
        pol["note"] = (
            f"{pol['note']}; 情绪温度计数据停在 {thermo.get('trade_date') or '?'} 日"
            f"(期望 {thermo.get('expected_asof') or '?'}), 本轮不据此条件化"
        )
        pol["temperature"] = {
            "seal_success_rate": thermo.get("seal_success_rate"),
            "blow_ratio": thermo.get("blow_ratio"),
            "trade_date": thermo.get("trade_date"),
            "stale": True,
        }
        return pol

    note_bits: list[str] = []
    if thermo.get("suspicious"):
        # SC15/SC3 出现 sick 值 → 温度不可信, 不据此条件化(显式留痕, 不猜)
        pol["note"] = f"{pol['note']}; 情绪温度计数据可疑(sick 值), 本轮不据此条件化"
        return pol

    from src.core import thresholds  # 局部导入避免循环依赖

    sr = thermo.get("seal_success_rate")
    br = thermo.get("blow_ratio")
    t_tighten = thresholds.value("seal_rate_tighten")
    t_loosen = thresholds.value("seal_rate_loosen")
    t_blow = thresholds.value("blow_rate_tighten")

    tighten = False
    loosen = False
    if sr is not None and sr < t_tighten:
        tighten = True
        note_bits.append(f"封板成功率 {sr * 100:.0f}%<阈值 {t_tighten * 100:.0f}% → 收紧")
    if br is not None and br >= t_blow:
        tighten = True
        note_bits.append(f"炸板比 {br * 100:.0f}%≥阈值 {t_blow * 100:.0f}% → 收紧")
    if not tighten and sr is not None and sr >= t_loosen:
        loosen = True
        note_bits.append(f"封板成功率 {sr * 100:.0f}%≥阈值 {t_loosen * 100:.0f}% → 放宽")

    if tighten:
        allow = tuple(r for r in pol["allow_touch_rows"] if r == 1) or (1,)
        pol["allow_touch_rows"] = allow
        pol["policy"] = "tighten"
    elif loosen:
        pol["allow_touch_rows"] = tuple(sorted(set(pol["allow_touch_rows"]) | {2}))
        pol["policy"] = "loosen"

    pol["thermo_applied"] = bool(tighten or loosen)
    pol["temperature"] = {
        "seal_success_rate": sr,
        "blow_ratio": br,
        "trade_date": thermo.get("trade_date"),
    }
    if note_bits:
        pol["note"] = f"{pol['note']}; 情绪温度计: " + "; ".join(note_bits)
    return pol


def condition_verdict(
    verdict: str, row: int | None, regime: str | None, *, thermo: dict[str, Any] | None = None
) -> tuple[str, bool, str | None]:
    """对『动手』档做情绪周期(+温度)条件化(纯函数)。

    `thermo`(可选, 2026-10-11 胜率证据层 ①): 情绪温度计(`current_thermometer`)结果。
    缺省 None → 与纯周期条件化逐字一致(向后兼容零破坏)。

    Returns:
        (verdict_after, adjusted, note)
        - tighten: 『动手』但状态表行不在允许集 → 收敛为『看看』;
        - loosen : 状态表行在允许集但基线不给『动手』(如拐点 行2) → 放开为『动手』;
        - normal / unknown: 与基线一致(向后兼容);
        - 只改『动手』档, 不动『别碰』。
    """
    pol = temperature_policy(regime, thermo)
    allowed = set(pol["allow_touch_rows"])
    label = pol["label"]
    r = row if isinstance(row, int) else None

    if verdict == "动手" and r is not None and r not in allowed:
        return (
            "看看",
            True,
            f"情绪周期条件化({label}/{pol['policy']}): 状态表行{r} 不在本态允许动手行 "
            f"{sorted(allowed)}, 『动手』收敛为『看看』({pol['note']})",
        )
    if verdict != "动手" and r is not None and r in allowed and r not in BASE_TOUCH_ROWS:
        return (
            "动手",
            True,
            f"情绪周期条件化({label}/{pol['policy']}): 状态表行{r} 在本态放宽行内, "
            f"放开为『动手』({pol['note']})",
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


# ── 历史 regime 回填(2026-10-11): 按【信号日】解析当日情绪周期态 ───────────────
# 背景: 存量 decision_log 的 regime 全 NULL(旧样本早于 regime 落库改动) → regime 分桶
# 退化为单桶, "53.85% 是否被状态平均掩盖"无法验证。本函数给存量行**按其信号日**补 regime。
# 口径铁律(禁自创第二套): 源优先级与映射**逐字复用** `_resolve`/`current_regime` ——
#   ① signal_summary_daily.snapshot_date 命中该日 → blocks.sentiment.cycle;
#   ② market_phase_daily.date 命中该日 → phase(经 PHASE_TO_REGIME);
#   ③ 都缺 → 'unknown'(显式缺数据, 不猜态)。
# 与 `current_regime` 的**唯一区别**: 按指定日精确取历史行, 而非取最新行。
# 温度计(market_sentiment_daily)只作**证据**一并返回, **不参与 regime key 判定**。


def _iso_day(day: Any) -> str:
    """日期归一到 ISO(YYYY-MM-DD) 仅供比较用(紧凑 20260918 与 ISO 2026-09-18 必须归一)。"""
    s = str(day).strip() if day is not None else ""
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _read_cycle_for_day(engine, day_iso: str, day_compact: str) -> tuple[str | None, str | None]:
    """读指定日 signal_summary_daily 的情绪周期 → (cycle, confidence)。缺/异常 → (None, None)。

    日期比较走 IN(ISO, 紧凑) 双字面量: signal_summary_daily.snapshot_date 语义是
    `YYYY-MM-DD`, 但账本 `trade_date` 是紧凑 `YYYYMMDD` —— 两者**都不可以**直接
    `==`(踩过回填 0 填), 故两种字面量都试。
    """
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT blocks FROM signal_summary_daily "
                    "WHERE snapshot_date IN (:a, :b) ORDER BY id DESC LIMIT 1"
                ),
                {"a": day_iso, "b": day_compact},
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 —— 表不存在/方言异常都算缺数据
        logger.debug("regime 回填: signal_summary 读取失败 %r", exc)
        return None, None
    if row is None:
        return None, None
    blocks = row[0]
    if isinstance(blocks, str):
        try:
            blocks = json.loads(blocks)
        except Exception:  # noqa: BLE001 —— 坏 JSON 按缺数据
            return None, None
    if not isinstance(blocks, dict):
        return None, None
    senti = blocks.get("sentiment") or {}
    if not isinstance(senti, dict):
        return None, None
    cycle = senti.get("cycle")
    if not cycle:
        return None, None
    return str(cycle), (str(senti.get("confidence")) if senti.get("confidence") else None)


def _read_phase_for_day(engine, day_iso: str, day_compact: str) -> str | None:
    """读指定日 market_phase_daily.phase(兜底源)。缺/异常 → None。"""
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            row = conn.execute(
                text('SELECT phase FROM market_phase_daily WHERE "date" IN (:a, :b) LIMIT 1'),
                {"a": day_iso, "b": day_compact},
            ).fetchone()
    except Exception as exc:  # noqa: BLE001
        # `date` 是 Date 列: 部分方言不接受紧凑字面量 → 退回只按 ISO 查(不猜)
        logger.debug("regime 回填: market_phase 读取失败(退回 ISO 单查) %r", exc)
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text('SELECT phase FROM market_phase_daily WHERE "date" = :a LIMIT 1'),
                    {"a": day_iso},
                ).fetchone()
        except Exception:  # noqa: BLE001
            return None
    if row is None:
        return None
    return str(row[0]) if row[0] else None


def _read_temperature_for_day(engine, day_iso: str, day_compact: str) -> dict[str, Any] | None:
    """读指定日 market_sentiment_daily 派生情绪温度计(证据, 不参与 regime 判定)。缺 → None。"""
    from sqlalchemy import text

    try:
        from src.core.sentiment_thermometer import derive_thermometer

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT trade_date, limit_up_count, limit_up_open_count, "
                    "seal_success_money, seal_fail_money FROM market_sentiment_daily "
                    "WHERE market = 'CN' AND trade_date IN (:a, :b) ORDER BY trade_date DESC LIMIT 1"
                ),
                {"a": day_compact, "b": day_iso},
            ).mappings().first()
    except Exception as exc:  # noqa: BLE001
        logger.debug("regime 回填: 温度计读取失败 %r", exc)
        return None
    return derive_thermometer(dict(row)) if row else None


def resolve_regime_for_day(engine, day: Any) -> dict[str, Any]:
    """按【信号日】解析当日情绪周期态(历史态回填口径)。**永不抛异常**。

    Returns: {available, regime, label, cycle, confidence, source, temperature[, reason]}
    - source ∈ {signal_summary, market_phase, none}; 都缺 → available=False + regime='unknown';
    - `temperature`: market_sentiment_daily 派生温度计(证据留存), 不参与 regime 判定。
    """
    day_iso = _iso_day(day)
    day_compact = day_iso.replace("-", "")
    base: dict[str, Any] = {
        "available": False,
        "regime": "unknown",
        "label": REGIME_LABELS["unknown"],
        "cycle": None,
        "confidence": None,
        "source": "none",
        "temperature": None,
    }
    if not day_iso:
        base["reason"] = "信号日不可解析"
        return base

    base["temperature"] = _read_temperature_for_day(engine, day_iso, day_compact)

    cycle, confidence = _read_cycle_for_day(engine, day_iso, day_compact)
    if cycle:
        key = normalize_regime(cycle)
        if key != "unknown":
            base.update(
                available=True, regime=key, label=REGIME_LABELS.get(key, key),
                cycle=cycle, confidence=confidence, source="signal_summary",
            )
            return base

    phase = _read_phase_for_day(engine, day_iso, day_compact)
    if phase:
        key = normalize_regime(phase)
        if key != "unknown":
            base.update(
                available=True, regime=key, label=REGIME_LABELS.get(key, key),
                cycle=phase, confidence=None, source="market_phase",
            )
            return base

    base["reason"] = "无当日情绪周期快照(signal_summary_daily / market_phase_daily 均无该日数据)"
    return base


def regime_for_day_key(engine, day: Any) -> str:
    """按【信号日】取规范 regime key(供账本回填); 解析不到 → 'unknown'(不猜态)。"""
    try:
        return resolve_regime_for_day(engine, day).get("regime") or "unknown"
    except Exception as exc:  # noqa: BLE001 —— 回填侧永不抛
        logger.debug("regime_for_day_key 解析失败(按 unknown): %r", exc)
        return "unknown"
