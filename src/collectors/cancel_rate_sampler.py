"""撤单率时序采样（2026-10-21）。

`md_more_info` 的撤单率是**当日会话累计**：只能回答"现在买撤 80%"，
回答不了"10:30 那波是不是突然拉起来"。要判断试盘/诱多，需要曲线的拐点。

做法：交易日 09:15–15:00 每 30 秒采一批自选股，按分钟幂等落 `cancel_rate_snapshots`；
标的轮转（每次 20 只），几分钟内覆盖全部自选，单次开销 ~2s（一次批量 md_more_info）。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

PER_TICK = 20
_WINDOW = ("0915", "1500")


def _hhmm(dt: datetime) -> str:
    return f"{dt.hour:02d}{dt.minute:02d}"


def _in_window(dt: datetime) -> bool:
    if dt.weekday() >= 5:
        return False
    return _WINDOW[0] <= _hhmm(dt) <= _WINDOW[1]


def targets(db: Session, limit: int = 200) -> list[str]:
    """采样标的：全用户自选股（去重），不够再补持仓。"""
    try:
        from src.db.models import Stock

        rows = db.query(Stock.symbol).distinct().limit(limit).all()
        syms = [str(r[0]).strip() for r in rows if r[0]]
    except Exception as exc:  # noqa: BLE001
        logger.debug("cancel sampler 取自选失败: %s", exc)
        return []
    if len(syms) >= limit:
        return syms[:limit]
    try:
        from src.db.models import Position

        prows = db.query(Position.symbol).distinct().limit(limit).all()
        for r in prows:
            s = str(r[0]).strip() if r[0] else ""
            if s and s not in syms:
                syms.append(s)
    except Exception:  # noqa: BLE001 - 持仓表名可能不同，缺了不影响主流程
        pass
    return syms[:limit]


def _rotate(syms: list[str], per_tick: int, now: datetime) -> list[str]:
    if len(syms) <= per_tick:
        return syms
    start = ((now.hour * 60 + now.minute) * per_tick) % len(syms)
    out = syms[start:start + per_tick]
    if len(out) < per_tick:
        out += syms[: per_tick - len(out)]
    return out


def collect_once(db: Session, per_tick: int = PER_TICK, now: datetime | None = None) -> int:
    """采一轮。返回写入/更新行数；非交易时段返回 0（不写脏数据）。"""
    now = now or datetime.now()
    if not _in_window(now):
        return 0
    syms = _rotate(targets(db), per_tick, now)
    if not syms:
        return 0

    from src.core.cancel_order_intent import calc_cancel_order_intent
    from src.core.marketdata_client import md_more_info
    from src.db.models import CancelRateSnapshot

    key_map = (
        ("BCancel", "cancel_buy"), ("SCancel", "cancel_sell"),
        ("TotalBVol", "total_buy_vol"), ("TotalSVol", "total_sell_vol"),
        ("L2OrderNum", "l2_order_num"), ("L2TicNum", "l2_tick_num"),
    )
    try:
        rows = md_more_info(syms, "CN")
    except Exception as exc:  # noqa: BLE001 - 源抖动不该打断调度
        logger.info("cancel sampler 取数失败: %s", exc)
        return 0

    today, hhmm, written = now.strftime("%Y%m%d"), _hhmm(now), 0
    for sym, row in zip(syms, rows or []):
        if not row:
            continue
        mi = {tq: row.get(low) for tq, low in key_map}
        r = calc_cancel_order_intent(mi, pct_chg=row.get("change_pct"))
        if not r.get("available"):
            continue  # 无有效撤单记录 → 不写 0 冒充
        # 撤单率源值是 0~1 小数，落库统一成百分数，避免下游又要乘一次
        payload = dict(
            buy_cancel_rate=round(float(r["b_cancel_ratio"]) * 100, 2),
            sell_cancel_rate=round(float(r["s_cancel_ratio"]) * 100, 2),
            cancel_bias=float(r.get("cancel_bias") or 0),
            intent=str(r.get("intent_tag") or "")[:24],
            source="tq_more_info",
            captured_at=now,
        )
        existing = (
            db.query(CancelRateSnapshot)
            .filter(CancelRateSnapshot.symbol == sym,
                    CancelRateSnapshot.trade_date == today,
                    CancelRateSnapshot.hhmm == hhmm)
            .first()
        )
        if existing:
            for k, v in payload.items():
                setattr(existing, k, v)
        else:
            db.add(CancelRateSnapshot(symbol=sym, trade_date=today, hhmm=hhmm, **payload))
        written += 1
    if written:
        db.commit()
    return written


def cancel_trend(db: Session, symbol: str, minutes: int = 40) -> str:
    """读最近一段采样，回答"撤单率在往哪个方向走"。"""
    from src.db.models import CancelRateSnapshot

    today = datetime.now().strftime("%Y%m%d")
    rows = (
        db.query(CancelRateSnapshot)
        .filter(CancelRateSnapshot.symbol == str(symbol),
                CancelRateSnapshot.trade_date == today)
        .order_by(CancelRateSnapshot.hhmm.desc())
        .limit(max(int(minutes), 2))
        .all()
    )
    if len(rows) < 2:
        return "[撤单趋势] 采样不足 2 个点（采集器刚上线或非交易时段），暂无趋势——不推断。"
    rows = list(reversed(rows))
    first, last = rows[0], rows[-1]

    def _delta(a: float | None, b: float | None) -> str:
        if a is None or b is None:
            return "--"
        d = b - a
        arrow = "↑" if d > 5 else ("↓" if d < -5 else "→")
        return f"{arrow}{d:+.1f}pt"

    return "\n".join([
        f"[撤单趋势] {symbol} 近 {len(rows)} 个采样（{first.hhmm}→{last.hhmm}，每 30s 采一批、按分钟落行）",
        f"买盘撤单率 {first.buy_cancel_rate:.1f}% → {last.buy_cancel_rate:.1f}%（{_delta(first.buy_cancel_rate, last.buy_cancel_rate)}）"
        f"｜卖盘 {first.sell_cancel_rate:.1f}% → {last.sell_cancel_rate:.1f}%（{_delta(first.sell_cancel_rate, last.sell_cancel_rate)}）",
        f"当前意图：{last.intent or '--'}｜撤单偏向 {last.cancel_bias:+.2f}"
        + ("（买方撤多）" if (last.cancel_bias or 0) > 0 else "（卖方撤多）" if (last.cancel_bias or 0) < 0 else ""),
    ])


def run_scheduled() -> None:
    """APScheduler 入口：自己开会话，不让调度线程持长连接。"""
    try:
        from src.db.session import SessionLocal
    except Exception:  # noqa: BLE001
        return
    db = SessionLocal()
    try:
        collect_once(db)
    except Exception as exc:  # noqa: BLE001 - 采样失败不该刷屏
        logger.info("cancel sampler tick 失败: %s", exc)
    finally:
        db.close()
