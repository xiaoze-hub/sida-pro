"""策略目录与权重读取。"""

from __future__ import annotations

from dataclasses import dataclass

from src.db.session import SessionLocal, WriteSessionLocal
from src.db.models import StrategyCatalog, StrategyWeight


@dataclass(frozen=True)
class StrategySpec:
    code: str
    name: str
    description: str
    version: str = "v1"
    enabled: bool = True
    market_scope: str = "ALL"
    risk_level: str = "medium"
    params: dict | None = None
    default_weight: float = 1.0


DEFAULT_STRATEGIES: tuple[StrategySpec, ...] = (
    StrategySpec(
        code="trend_follow",
        name="趋势延续",
        description="顺势跟随，优先均线多头且动量延续",
        risk_level="medium",
        params={"horizon_days": 5},
        default_weight=1.15,
    ),
    StrategySpec(
        code="macd_golden",
        name="MACD金叉",
        description="MACD 金叉确认，偏中短线",
        risk_level="medium",
        params={"horizon_days": 3},
        default_weight=1.10,
    ),
    StrategySpec(
        code="volume_breakout",
        name="放量突破",
        description="放量突破关键位，偏进攻",
        risk_level="high",
        params={"horizon_days": 3},
        default_weight=1.18,
    ),
    StrategySpec(
        code="pullback",
        name="回踩确认",
        description="回踩支撑后二次启动",
        risk_level="low",
        params={"horizon_days": 5},
        default_weight=1.05,
    ),
    StrategySpec(
        code="rebound",
        name="超跌反弹",
        description="超跌后的反弹交易",
        risk_level="high",
        params={"horizon_days": 3},
        default_weight=0.95,
    ),
    StrategySpec(
        code="watchlist_agent",
        name="Agent建议",
        description="来自既有 Agent 的综合建议映射",
        risk_level="medium",
        params={"horizon_days": 3},
        default_weight=1.00,
    ),
    StrategySpec(
        code="market_scan",
        name="市场扫描",
        description="市场池扫描策略（热门与活跃）",
        risk_level="medium",
        params={"horizon_days": 3},
        default_weight=1.08,
    ),
    StrategySpec(
        code="sentiment_adaptive",
        name="情绪周期自适应",
        description="冰点试错→修复→发酵→高潮→退潮，五阶段动态仓位",
        risk_level="medium",
        params={"horizon_days": 5},
        default_weight=1.12,
    ),
    StrategySpec(
        code="event_catalyst",
        name="事件预期差",
        description="公告/快讯催化预期差，LLM受益链抢先",
        risk_level="high",
        params={"horizon_days": 3},
        default_weight=1.15,
    ),
    StrategySpec(
        code="dark_flow_tracking",
        name="暗盘资金跟随",
        description="暗盘流入+明盘流出拆单吸筹潜伏",
        risk_level="medium",
        params={"horizon_days": 5},
        default_weight=1.18,
    ),
)


def ensure_strategy_catalog(db=None) -> None:
    """补齐默认策略目录行(幂等)。

    - `db=None`: 自开写会话并自行 commit(读路径的默认用法);
    - 传入会话时**不 commit、不 close** —— 事务边界交给调用方, 以便与随后的
      权重写入落在**同一个事务**里(2026-11-12 复审二: 目录读取原先各自开
      会话, 读的是副本/别的事务, 与写权重的 before 值可能不是同一时点)。

    「先查有没有再插入」必须全程在写库: 路由会话的 SELECT 会把事务绑到读副本,
    随后 INSERT 落错引擎(只读副本报错 / 分裂成两条连接)。
    """
    own = db is None
    if own:
        db = WriteSessionLocal()
    try:
        changed = False
        for spec in DEFAULT_STRATEGIES:
            row = (
                db.query(StrategyCatalog)
                .filter(StrategyCatalog.code == spec.code)
                .first()
            )
            if not row:
                db.add(
                    StrategyCatalog(
                        code=spec.code,
                        name=spec.name,
                        description=spec.description,
                        version=spec.version,
                        enabled=bool(spec.enabled),
                        market_scope=spec.market_scope,
                        risk_level=spec.risk_level,
                        params=spec.params or {},
                        default_weight=float(spec.default_weight),
                    )
                )
                changed = True
                continue
            if row.name != spec.name:
                row.name = spec.name
                changed = True
            if (row.description or "") != (spec.description or ""):
                row.description = spec.description
                changed = True
            if (row.version or "v1") != (spec.version or "v1"):
                row.version = spec.version
                changed = True
            if (row.market_scope or "ALL") != (spec.market_scope or "ALL"):
                row.market_scope = spec.market_scope
                changed = True
            if (row.risk_level or "medium") != (spec.risk_level or "medium"):
                row.risk_level = spec.risk_level
                changed = True
            if float(row.default_weight or 1.0) != float(spec.default_weight):
                row.default_weight = float(spec.default_weight)
                changed = True
            if not row.params:
                row.params = spec.params or {}
                changed = True
        if changed:
            if own:
                db.commit()
            else:
                # 调用方持有事务: flush 让后续读看到新行, commit 由调用方决定。
                db.flush()
    finally:
        if own:
            db.close()


def list_strategy_catalog(enabled_only: bool = True, *, db=None) -> list[dict]:
    """列出策略目录。

    传入 `db` 时**不**自行开会话、也不做 ensure 写入 —— 调用方负责在同一事务里
    先 `ensure_strategy_catalog(db=db)`(调权路径就是这样, 让「读目录 + 读结果 +
    写权重 + 写审计」同事务)。默认路径保持原行为(自开读会话 + 先 ensure)。
    """
    own = db is None
    if own:
        ensure_strategy_catalog()
        db = SessionLocal()
    try:
        q = db.query(StrategyCatalog)
        if enabled_only:
            q = q.filter(StrategyCatalog.enabled.is_(True))
        rows = q.order_by(StrategyCatalog.code.asc()).all()
        out = []
        for r in rows:
            out.append(
                {
                    "code": r.code,
                    "name": r.name,
                    "description": r.description or "",
                    "version": r.version or "v1",
                    "enabled": bool(r.enabled),
                    "market_scope": r.market_scope or "ALL",
                    "risk_level": r.risk_level or "medium",
                    "params": r.params or {},
                    "default_weight": float(r.default_weight or 1.0),
                }
            )
        return out
    finally:
        if own:
            db.close()


def get_strategy_profile_map() -> dict[str, dict]:
    rows = list_strategy_catalog(enabled_only=False)
    return {x["code"]: x for x in rows}


def get_effective_weight_map(
    *, market: str = "ALL", regime: str = "default", as_of=None
) -> dict[str, float]:
    """有效策略权重映射。as_of(B0.5): 传入 datetime 时按 `StrategyWeightHistory`
    取该时点之前最近一次生效的权重(历史快照可复现), 无历史则回落当前值。"""
    ensure_strategy_catalog()
    mkt = (market or "ALL").strip().upper() or "ALL"
    reg = (regime or "default").strip() or "default"
    db = SessionLocal()
    try:
        defaults = {
            s.code: float(s.default_weight or 1.0)
            for s in db.query(StrategyCatalog).all()
        }
        if as_of is not None:
            from src.db.models import StrategyWeightHistory

            hist = (
                db.query(StrategyWeightHistory)
                .filter(
                    StrategyWeightHistory.regime == reg,
                    StrategyWeightHistory.market.in_(("ALL", mkt)),
                    StrategyWeightHistory.created_at <= as_of,
                )
                .order_by(StrategyWeightHistory.created_at.asc())
                .all()
            )
            out = dict(defaults)
            for h in hist:
                key = (h.strategy_code or "").strip()
                if not key:
                    continue
                if (h.market or "ALL").upper() == mkt:
                    out[key] = float(h.new_weight or out.get(key, 1.0))
                elif key not in out:
                    out[key] = float(h.new_weight or 1.0)
            return out
        rows = (
            db.query(StrategyWeight)
            .filter(
                StrategyWeight.regime == reg,
                StrategyWeight.market.in_(("ALL", mkt)),
            )
            .all()
        )
        out = dict(defaults)
        for r in rows:
            key = (r.strategy_code or "").strip()
            if not key:
                continue
            # Market-specific weight overrides ALL.
            if (r.market or "ALL").upper() == mkt:
                out[key] = float(r.weight or out.get(key, 1.0))
            elif key not in out:
                out[key] = float(r.weight or 1.0)
        return out
    finally:
        db.close()
