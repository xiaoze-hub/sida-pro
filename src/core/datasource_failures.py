"""数据源失败明细(B1.6/KI-040)。

Prometheus 计数器只有聚合曲线, 无法定位"哪个源、哪只票、哪次请求"。
本模块把失败落成可查明细, 但**必须限流** —— vendor 层失败可能高频,
否则会把库写爆。策略: 同一 (provider, kind) 60 秒内最多落 1 行。
"""

from __future__ import annotations

import logging
import time

from src.web.database import SessionLocal
from src.web.models import DatasourceFailure

logger = logging.getLogger(__name__)

_KINDS = ("fetch", "parse", "timeout", "auth")
_DEDUPE_WINDOW_SEC = 60.0
_last_persist: dict[tuple[str, str], float] = {}


def _should_persist(provider: str, kind: str, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    key = (provider, kind)
    last = _last_persist.get(key)
    if last is not None and now - last < _DEDUPE_WINDOW_SEC:
        return False
    _last_persist[key] = now
    return True


def record(
    provider: str,
    kind: str = "fetch",
    *,
    symbol: str = "",
    detail: str = "",
    latency_ms: int | None = None,
    db=None,
) -> bool:
    """落一条失败明细(限流); 返回是否真的落库。异常一律吞掉, 不影响主流程。"""
    try:
        prov = str(provider or "").strip() or "unknown"
        k = kind if kind in _KINDS else "fetch"
        if not _should_persist(prov, k):
            return False
        own = db is None
        db = db or SessionLocal()
        try:
            db.add(
                DatasourceFailure(
                    provider=prov,
                    kind=k,
                    symbol=str(symbol or "")[:32],
                    detail=str(detail or "")[:500],
                    latency_ms=int(latency_ms) if latency_ms is not None else None,
                )
            )
            db.commit()
            return True
        finally:
            if own:
                db.close()
    except Exception as e:  # noqa: BLE001 - 明细落库绝不影响业务
        logger.debug("[数据源明细] 落库失败: %s", e)
        return False


def recent(*, limit: int = 50, provider: str | None = None, db=None) -> list[dict]:
    """最近失败明细(倒序), 供 API/排查使用。"""
    own = db is None
    db = db or SessionLocal()
    try:
        q = db.query(DatasourceFailure)
        if provider:
            q = q.filter(DatasourceFailure.provider == str(provider))
        rows = (
            q.order_by(DatasourceFailure.created_at.desc(), DatasourceFailure.id.desc())
            .limit(max(1, int(limit)))
            .all()
        )
        return [
            {
                "provider": r.provider,
                "kind": r.kind,
                "symbol": r.symbol or "",
                "detail": r.detail or "",
                "latency_ms": r.latency_ms,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    finally:
        if own:
            db.close()


def reset_dedupe() -> None:
    """测试用: 清空限流窗口。"""
    _last_persist.clear()
