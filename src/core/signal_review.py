"""信号→复盘闭环(批次D, 2026-09-06 28号)。

闭环: 信号 emit 时快照落库(signal_snapshots) → 每晚对账 job 回填
T+1/T+5 收盘收益 → 命中率查询(每信号类型胜率/均值, 对照官方基准标注)。

口径(诚实标注):
- T+h = 事件日收盘 → 之后第 h 个**交易日**收盘(日K序列顺延, 非自然日);
- 事件日当日无 K 线(新股/停牌) → 该样本 checked 置 1 但 outcome 留 None
  (显式无数据, 不计入胜率分母也不编造);
- 官方基准 75.42%/3.45 仅适用于"三指标共振向好"口径对照, 其他信号类型
  无官方基准, 报告只给自身统计。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")
_TABLE = "signal_snapshots"

# 官方对照基准(决策先锋 8问8答, 2024.10-2025.10): 仅 resonance 类信号参考
OFFICIAL_BENCHMARK = {"resonance": {"win_rate": 75.42, "pl_ratio": 3.45}}

KNOWN_SIGNAL_TYPES = ("seal_quality", "ambush_candidate", "gs_signal", "resonance", "dark_cluster")


def record_signal(
    signal_type: str,
    symbol: str,
    direction: str | None = None,
    strength: float | None = None,
    payload: dict | None = None,
    emit_date: str | None = None,
) -> bool:
    """信号快照落库。同 (type, symbol, emit_date) 首条才写(幂等)。永不抛异常。"""
    if signal_type not in KNOWN_SIGNAL_TYPES:
        logger.warning("record_signal 未知信号类型 %s", signal_type)
        return False
    now = datetime.now(_CST)
    emit_date = emit_date or now.strftime("%Y%m%d")
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        exists = db.execute(
            text(f"SELECT 1 FROM {_TABLE} WHERE signal_type = :t AND symbol = :s AND emit_date = :d"),
            {"t": signal_type, "s": symbol, "d": emit_date},
        ).first()
        if exists:
            return False
        db.execute(
            text(
                f"INSERT INTO {_TABLE} (emit_ts, emit_date, signal_type, symbol, market, direction,"
                " strength, payload) VALUES (:ts, :d, :t, :s, 'CN', :dir, :st, :pl)"
            ),
            {
                "ts": now.isoformat(timespec="seconds"),
                "d": emit_date,
                "t": signal_type,
                "s": symbol,
                "dir": direction,
                "st": strength,
                "pl": json.dumps(payload or {}, ensure_ascii=False),
            },
        )
        db.commit()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("record_signal %s/%s 失败: %s", signal_type, symbol, e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False
    finally:
        db.close()


def _future_close(closes: dict[str, float], emit_date: str, h: int) -> tuple[float | None, str | None]:
    """emit_date 之后第 h 个交易日的收盘。纯函数。

    closes: {YYYYMMDD: close}。emit_date 本身不在序列/无其后数据 → (None, None)。
    """
    if emit_date not in closes:
        return None, None
    dates = sorted(d for d in closes if d > emit_date)
    if len(dates) < h:
        return None, None
    key = dates[h - 1]
    return closes[key], key


def _load_closes(symbol: str) -> dict[str, float]:
    try:
        from src.core.decision_pioneer import fetch_bars

        bars = fetch_bars(symbol, "CN", days=40)
        return {
            str(b.get("date")).replace("-", ""): float(b["close"])
            for b in bars
            if b.get("close") and b.get("date")
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_review 取K线 %s 失败: %s", symbol, e)
        return {}


def nightly_review(days_back: int = 3) -> dict:
    """每晚对账(默认回看 3 天未检查样本): 回填 T+1/T+5 收盘收益。"""
    from src.web.database import SessionLocal

    cutoff = (datetime.now(_CST) - timedelta(days=days_back)).strftime("%Y%m%d")
    db = SessionLocal()
    stats = {"checked_t1": 0, "checked_t5": 0, "no_data": 0}
    try:
        rows = db.execute(
            text(
                f"SELECT emit_ts, emit_date, symbol, outcome_t1, outcome_t5, checked_t1, checked_t5"
                f" FROM {_TABLE} WHERE emit_date >= :cutoff"
            ),
            {"cutoff": cutoff},
        ).fetchall()
        closes_cache: dict[str, dict[str, float]] = {}
        for r in rows:
            d = dict(r._mapping)
            if d["symbol"] not in closes_cache:
                closes_cache[d["symbol"]] = _load_closes(d["symbol"])
            closes = closes_cache[d["symbol"]]
            base = closes.get(d["emit_date"])
            if base is None:
                # 事件日无收盘(停牌/新股), 标记已查但无结果(显式无数据)
                if not d["checked_t1"]:
                    db.execute(
                        text(f"UPDATE {_TABLE} SET checked_t1 = 1 WHERE emit_ts = :ts"),
                        {"ts": d["emit_ts"]},
                    )
                    stats["no_data"] += 1
                if not d["checked_t5"]:
                    db.execute(
                        text(f"UPDATE {_TABLE} SET checked_t5 = 1 WHERE emit_ts = :ts"),
                        {"ts": d["emit_ts"]},
                    )
                continue
            if not d["checked_t1"]:
                c1, _ = _future_close(closes, d["emit_date"], 1)
                if c1 is not None:
                    db.execute(
                        text(f"UPDATE {_TABLE} SET outcome_t1 = :o, checked_t1 = 1 WHERE emit_ts = :ts"),
                        {"o": round((c1 - base) / base * 100, 3), "ts": d["emit_ts"]},
                    )
                    stats["checked_t1"] += 1
            if not d["checked_t5"]:
                c5, _ = _future_close(closes, d["emit_date"], 5)
                if c5 is not None:
                    db.execute(
                        text(f"UPDATE {_TABLE} SET outcome_t5 = :o, checked_t5 = 1 WHERE emit_ts = :ts"),
                        {"o": round((c5 - base) / base * 100, 3), "ts": d["emit_ts"]},
                    )
                    stats["checked_t5"] += 1
        db.commit()
        logger.info("信号对账完成: %s", stats)
        return stats
    except Exception as e:  # noqa: BLE001
        logger.warning("信号对账失败: %s", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"error": str(e)}
    finally:
        db.close()


def hit_rate(signal_type: str | None = None, days: int = 30) -> dict:
    """命中率统计: 按信号类型聚合 T+1/T+5 胜率与均值(方向按 long 归一)。"""
    from src.web.database import SessionLocal

    cutoff = (datetime.now(_CST) - timedelta(days=days)).strftime("%Y%m%d")
    db = SessionLocal()
    try:
        if signal_type:
            rows = db.execute(
                text(
                    f"SELECT signal_type, symbol, direction, outcome_t1, outcome_t5 FROM {_TABLE}"
                    " WHERE emit_date >= :cutoff AND signal_type = :t"
                ),
                {"cutoff": cutoff, "t": signal_type},
            ).fetchall()
        else:
            rows = db.execute(
                text(
                    f"SELECT signal_type, symbol, direction, outcome_t1, outcome_t5 FROM {_TABLE}"
                    " WHERE emit_date >= :cutoff"
                ),
                {"cutoff": cutoff},
            ).fetchall()
    finally:
        db.close()

    agg: dict[str, dict[str, list]] = {}
    for r in rows:
        d = dict(r._mapping)
        st = d["signal_type"]
        slot = agg.setdefault(st, {"t1": [], "t5": []})
        sign = -1.0 if (d.get("direction") == "short") else 1.0
        if d.get("outcome_t1") is not None:
            slot["t1"].append(sign * float(d["outcome_t1"]))
        if d.get("outcome_t5") is not None:
            slot["t5"].append(sign * float(d["outcome_t5"]))

    def _pack(xs: list[float]) -> dict:
        if not xs:
            return {"n": 0, "win_rate": None, "avg_pct": None}
        return {
            "n": len(xs),
            "win_rate": round(sum(1 for x in xs if x > 0) / len(xs) * 100, 2),
            "avg_pct": round(sum(xs) / len(xs), 3),
        }

    by_type = {k: {"t1": _pack(v["t1"]), "t5": _pack(v["t5"])} for k, v in agg.items()}
    for k, v in by_type.items():
        bench = OFFICIAL_BENCHMARK.get(k)
        if bench and v["t5"]["n"] >= 20:
            v["official_benchmark"] = {
                **bench,
                "note": f"官方口径(共振向好 T5 上涨概率 {bench['win_rate']}%); 本站 n={v['t5']['n']} 供对照, 口径不完全一致",
            }
    return {"days": days, "by_type": by_type}
