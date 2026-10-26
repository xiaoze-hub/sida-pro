"""指数日线读取（index_daily，2026-10-25）。

个股走 `klines` hyprtable，指数走 `index_daily` —— 两条表不能混。
000001 在 klines 里是**平安银行**，在 index_daily 里是**上证指数**，
这就是所有「指数被当成个股」事故的总根源。

所以凡是要读「日线序列」的核心模块，都先过 `is_index_code()` 判一次，
再调这里或 klines_repo。别再各写一份。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

_SQL = (
    "SELECT trade_date, open, high, low, close, volume, amount, name FROM index_daily "
    "WHERE code = :code AND trade_date >= :since ORDER BY trade_date ASC"
)


def load_bars(code: str, days: int = 250, *, session: Any = None) -> list[dict[str, Any]]:
    """读指数日线（升序）。失败返回 []，不抛（调用方按「无数据」处理）。

    自然日窗口取 days*2，再按交易日截尾 —— 交易日 ≈ 自然日 × 0.68，取 2 倍安全。
    """
    from src.db.session import SessionLocal

    code = str(code or "").strip()
    if not code:
        return []
    days = max(int(days or 250), 2)
    since = (date.today() - timedelta(days=days * 2)).isoformat()
    own = session is None
    db = SessionLocal() if own else session
    try:
        rows = db.execute(text(_SQL), {"code": code, "since": since}).mappings().all()
    except Exception as e:  # noqa: BLE001 — 读库失败不该炸掉调用方
        logger.warning("index_daily 读取失败 %s: %s", code, e)
        return []
    finally:
        if own:
            db.close()

    bars = [
        {
            "date": str(r["trade_date"])[:10],
            "open": r["open"],
            "high": r["high"],
            "low": r["low"],
            "close": r["close"],
            "volume": r["volume"],
            "amount": r["amount"],
            "name": r["name"],
        }
        for r in rows
    ]
    return bars[-days:]
