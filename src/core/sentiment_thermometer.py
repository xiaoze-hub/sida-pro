# -*- coding: utf-8 -*-
"""情绪温度计(2026-10-11 胜率证据层 ①): 把 SC15/SC3 派生的『封板成功率 / 炸板比』
读出来, 供情绪周期条件化门槛解释"为什么此刻收紧/放宽"。

## 为什么
`market_regime.condition_verdict` 已按**五态**(冰点/修复/发酵/高潮/退潮)条件化『动手』档,
但同一态内部的冷热差异(封板是否好打、炸板是否泛滥)还没有进入判据。温度计把这个
**日频市场温度**读出来: 封板成功率高→承接强(可放宽), 低→资金退却(该收紧);
炸板比高→分歧加剧(该收紧)。**只加维度、不造数据**。

## 数据源(零新采集: market_sentiment_daily 全量落库, tq_sentiment_series)
- **封板成功率** = SC15.seal_success_money / (seal_success_money + seal_fail_money)
  (打板成功资金 / (成功+失败) 资金; 0-1)。
- **炸板比** = SC3.limit_up_open_count / SC3.limit_up_count
  (涨停打开/炸板家数 / 涨停家数; 0-1)。两者皆**派生自已落库列**, 不新增采集。

## 诚实纪律(与全仓一致)
- 读不到 / 分母为 0 / 列为空 → 该指标 `None`, **不编造**, 由调用方按标准口径放行;
- **sick 值**(负 / 非有限) → `suspicious=True`, 该指标**不硬算**(不拿异常值当温度);
- 阈值一律走 `src.core.thresholds`(禁硬编码), 由条件化消费方读取。
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")


def _num(v: Any) -> float | None:
    """任意输入 → float 或 None。空串/None/非数 → None(不用 0 冒充缺失)。"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _is_sick(v: Any) -> bool:
    """sick 值判定: 非空但非法(负 / 非有限) → True。

    区分『没有数据(None)』与『有数据但明显异常(负数/NaN)』 —— 后者要显式打
    `suspicious`, 不许当成真实温度硬算。
    """
    if v is None or v == "":
        return False
    try:
        f = float(v)
    except (TypeError, ValueError):
        return True
    return (not math.isfinite(f)) or f < 0


def _stale_days(trade_date: str, expected_asof: str) -> int | None:
    """trade_date 相对 expected_asof 落后多少个自然日; 不可比 → None(不猜)。"""
    from datetime import date

    try:
        d = datetime.strptime(str(trade_date), "%Y%m%d").date()
        e = datetime.strptime(str(expected_asof), "%Y%m%d").date()
    except (TypeError, ValueError):
        return None
    return (e - d).days


def derive_thermometer(row: Any, *, expected_asof: str | None = None) -> dict[str, Any]:
    """一行 market_sentiment_daily(映射/对象) → 情绪温度计(纯函数, 无 IO)。

    ``expected_asof``(可选, 2026-10-11 断链修复): 期望的最新交易日(YYYYMMDD)。
    传入且行日期落后其上时 → ``stale=True`` + ``reason`` 写明『数据停在 X 日』,
    **供消费方显式标注, 不拿旧值冒充当日**。缺省 None → 与旧行为逐字段一致。

    Returns:
        {
          available: bool,              # 至少一个指标可算
          trade_date: str,
          stale: bool,                  # 数据停在过去(仅当传入 expected_asof 才可能为真)
          stale_days: int|None,         # 落后自然日数
          expected_asof: str|None,
          seal_success_rate: float|None,  # 封板成功率 0-1
          blow_ratio: float|None,         # 炸板比 0-1
          seal_success_money, seal_fail_money, limit_up_count, limit_up_open_count: float|None,
          suspicious: bool,             # 出现 sick 值(负/非有限)
          reason: str,                  # 说明缺哪一维 / 为什么不可用
        }
    """
    def g(k: str) -> Any:
        if isinstance(row, dict):
            return row.get(k)
        return getattr(row, k, None) if row is not None else None

    td = g("trade_date")
    td = str(td) if td is not None else ""

    ss_raw, sf_raw = g("seal_success_money"), g("seal_fail_money")
    lu_raw, lo_raw = g("limit_up_count"), g("limit_up_open_count")

    suspicious = any(_is_sick(x) for x in (ss_raw, sf_raw, lu_raw, lo_raw))

    seal_success_money = _num(ss_raw)
    seal_fail_money = _num(sf_raw)
    limit_up_count = _num(lu_raw)
    limit_up_open_count = _num(lo_raw)

    reasons: list[str] = []

    # ── 封板成功率 ──
    seal_success_rate: float | None = None
    if _is_sick(ss_raw) or _is_sick(sf_raw):
        reasons.append("SC15 打板资金出现异常值(sick), 不硬算封板成功率")
    elif seal_success_money is None and seal_fail_money is None:
        reasons.append("SC15 打板资金缺数据")
    else:
        denom = (seal_success_money or 0.0) + (seal_fail_money or 0.0)
        if denom <= 0:
            reasons.append("SC15 打板资金合计为 0, 无法算封板成功率")
        else:
            seal_success_rate = (seal_success_money or 0.0) / denom

    # ── 炸板比 ──
    blow_ratio: float | None = None
    if _is_sick(lu_raw) or _is_sick(lo_raw):
        reasons.append("SC3 涨停/炸板家数出现异常值(sick), 不硬算炸板比")
    elif limit_up_count is None or limit_up_count <= 0:
        reasons.append("SC3 涨停家数为空/为 0, 无法算炸板比")
    elif limit_up_open_count is None:
        reasons.append("SC3 炸板(打开)家数缺数据")
    else:
        blow_ratio = limit_up_open_count / limit_up_count

    available = seal_success_rate is not None or blow_ratio is not None

    # ── 陈旧标注(数据停在过去) ──
    stale = False
    sd: int | None = None
    if expected_asof and td:
        sd = _stale_days(td, expected_asof)
        if sd is not None and sd > 0:
            stale = True
            reasons.append(f"数据停在 {td} 日(期望 {expected_asof}, 落后 {sd} 天)")

    if reasons:
        reasons_text = "; ".join(reasons)
    else:
        reasons_text = ""
    return {
        "available": available,
        "trade_date": td,
        "stale": stale,
        "stale_days": sd,
        "expected_asof": expected_asof,
        "seal_success_rate": seal_success_rate,
        "blow_ratio": blow_ratio,
        "seal_success_money": seal_success_money,
        "seal_fail_money": seal_fail_money,
        "limit_up_count": limit_up_count,
        "limit_up_open_count": limit_up_open_count,
        "suspicious": suspicious,
        "reason": reasons_text,
    }


# 进程内短缓存(温度按日刷新; 60s 足够, 避免决策热路径每标的都查库)
_MEMO_TTL_S = 60.0
_memo_lock = threading.Lock()
_memo: dict[str, Any] = {"ts": 0.0, "value": None}


def _read_latest_row(db) -> dict[str, Any] | None:
    """读最新一行 market_sentiment_daily(CN)。缺表/异常 → None(不抛)。"""
    from sqlalchemy import text

    try:
        row = db.execute(
            text(
                "SELECT trade_date, limit_up_count, limit_up_open_count, "
                "seal_success_money, seal_fail_money FROM market_sentiment_daily "
                "WHERE market = 'CN' ORDER BY trade_date DESC LIMIT 1"
            )
        ).mappings().first()
    except Exception as e:  # noqa: BLE001 —— 表不存在/方言异常都算缺数据
        logger.debug("情绪温度计读取失败: %r", e)
        return None
    return dict(row) if row else None


def _expected_asof() -> str | None:
    """期望的最新交易日(YYYYMMDD); 日历不可用 → None(不猜, 也就不标陈旧)。"""
    try:
        from src.core.sentiment_freshness import _expected_latest

        return _expected_latest()
    except Exception as e:  # noqa: BLE001 —— 拿不到期望日就不标"陈旧"
        logger.debug("期望交易日解析失败(不标陈旧): %r", e)
        return None


def _resolve(db=None) -> dict[str, Any]:
    """读当前情绪温度计(不走网络)。永远返回 dict; 失败 → available=False + reason。"""
    from src.db.session import SessionLocal

    expected = _expected_asof()
    owns = db is None
    session = db
    try:
        if session is None:
            session = SessionLocal()
        try:
            row = _read_latest_row(session)
        finally:
            if owns and session is not None:
                session.close()
        if row is None:
            return {
                "available": False,
                "trade_date": "",
                "stale": True,
                "stale_days": None,
                "expected_asof": expected,
                "seal_success_rate": None,
                "blow_ratio": None,
                "suspicious": False,
                "reason": "无情绪温度数据(market_sentiment_daily 无 CN 行)",
            }
        return derive_thermometer(row, expected_asof=expected)
    except Exception as e:  # noqa: BLE001
        return {
            "available": False,
            "trade_date": "",
            "stale": True,
            "stale_days": None,
            "expected_asof": expected,
            "seal_success_rate": None,
            "blow_ratio": None,
            "suspicious": False,
            "reason": f"读取失败: {e}",
        }


def current_thermometer(db=None, *, use_memo: bool = True) -> dict[str, Any]:
    """当前情绪温度计(带 60s 进程内缓存)。

    available=False + 各指标 None = 显式缺数据(caller 不做温度条件化, 按标准口径放行)。
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


def clear_thermometer_memo() -> None:
    """清进程内缓存(测试/运维用)。"""
    with _memo_lock:
        _memo["ts"] = 0.0
        _memo["value"] = None


def asof_today() -> str:
    """当日 CST 日期(ISO), 供展示。"""
    return datetime.now(_CST).date().isoformat()
