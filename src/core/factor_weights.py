"""因子权重存取层(M1)。

把信号合成时各因子的权重从「隐式 = 1 的硬编码」变成「外置 + 可标定」:
- `get_factor_weights(market)`:读取某市场各因子权重,缺失则 lazy seed 为 1.0;
  供 `strategy_engine._compute_factor_breakdown` 合成 raw_score 时按因子相乘。

标定逻辑见 `factor_calibration.py`;只读/手动覆盖的 API 在 `web/api/factors.py`。

口径提示(2026-11-12):
- `meta.last_ic / last_ir` 是**最后一次记录下来的历史观测**, 不是实时值;
  `_serialize` 额外给出 `last_observation_at / last_observation_stale /
  observation_note`, 调用方不得把它当「现在算出来的 IC」展示。
- 手动改权重是全局操作, actor 必须由 web 层从服务端认证身份传入
  (`require_settings_write`), 与改动同一次 commit 落进历史行 meta。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from src.core.timezone import utc_now
from src.db.session import SessionLocal
from src.db.models import FactorWeight, FactorWeightHistory

logger = logging.getLogger(__name__)

# 可标定因子(与 StrategyFactorSnapshot 列、factor_eval.FACTOR_FIELDS 对齐)。
# source_bonus 暂不进标定集(v1 维持权重 1.0);final_score 是合成结果,非输入因子。
CALIBRATABLE_FACTORS = (
    "alpha_score",
    "catalyst_score",
    "quality_score",
    "risk_penalty",
    "crowd_penalty",
)

# 惩罚类因子:在 raw_score 中被减,IC 预期为负。
PENALTY_FACTORS = frozenset({"risk_penalty", "crowd_penalty"})

MARKETS = ("CN", "HK", "US")

#: `meta.last_ic` 超过这么久没被刷新 ⇒ 标成历史观测(不允许冒充当前值)。
#: 只读标定不落库, 所以这个时间戳只代表「最后一次被记录的时刻」。
OBSERVATION_STALE_AFTER = timedelta(days=7)


def _parse_observed_at(raw) -> datetime | None:
    """把 meta 里的 ISO 时间串解析成带时区 datetime; 解析不了就当没有。"""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def get_factor_weights(market: str, *, as_of=None, db=None) -> dict[str, float]:
    """读取某市场各可标定因子的权重;缺失因子 lazy seed 为 1.0。

    as_of(B0.5, 2026-09-09): 传入 datetime 时按 `FactorWeightHistory` 取该时点**之前**
    最近一次生效的权重(历史快照可复现, 不套用今天的权重); 无历史记录则回落当前值。
    返回 {factor_code: weight},键恒为 CALIBRATABLE_FACTORS 全集。
    """
    own = db is None
    db = db or SessionLocal()
    try:
        rows = db.query(FactorWeight).filter(FactorWeight.market == market).all()
        existing = {r.factor_code: float(r.weight) for r in rows}
        if as_of is not None:
            hist = (
                db.query(FactorWeightHistory)
                .filter(
                    FactorWeightHistory.market == market,
                    FactorWeightHistory.created_at <= as_of,
                )
                .order_by(FactorWeightHistory.created_at.asc())
                .all()
            )
            asof_map: dict[str, float] = {}
            for h in hist:  # 升序遍历, 后写覆盖前 → 取 as_of 前最后一次
                if h.factor_code:
                    asof_map[h.factor_code] = float(h.new_weight or 1.0)
            return {f: asof_map.get(f, existing.get(f, 1.0)) for f in CALIBRATABLE_FACTORS}
        missing = [f for f in CALIBRATABLE_FACTORS if f not in existing]
        if missing:
            for f in missing:
                db.add(FactorWeight(factor_code=f, market=market, weight=1.0))
            db.commit()
            for f in missing:
                existing[f] = 1.0
        return {f: existing.get(f, 1.0) for f in CALIBRATABLE_FACTORS}
    finally:
        if own:
            db.close()


def _serialize(row: FactorWeight) -> dict:
    meta = row.meta or {}
    observed_at = meta.get("last_calibrated_at")
    observed_dt = _parse_observed_at(observed_at)
    stale = bool(
        observed_dt is None
        or (utc_now() - observed_dt) > OBSERVATION_STALE_AFTER
    )
    return {
        "factor_code": row.factor_code,
        "market": row.market,
        "weight": round(float(row.weight), 4),
        "is_pinned": bool(row.is_pinned),
        "auto_calibrate": bool(row.auto_calibrate),
        "last_ic": meta.get("last_ic"),
        "last_ir": meta.get("last_ir"),
        "last_sample_size": meta.get("last_sample_size"),
        "last_calibrated_at": meta.get("last_calibrated_at"),
        # 显式把 last_* 定性为**历史观测**: 带观测时间 + 是否过期,
        # 免得调用方把几周前的 meta 当成「现在算出来的 IC」。
        "last_observation_at": observed_at,
        "last_observation_stale": stale,
        "observation_note": (
            "历史观测(meta 记录, 已超过 "
            f"{OBSERVATION_STALE_AFTER.days} 天或从未记录), 非当前实时值; "
            "实时只读建议见 GET /api/factors/calibration"
            if stale
            else "历史观测(meta 记录, 非当前实时值); 实时只读建议见 GET /api/factors/calibration"
        ),
        "reason": row.reason or "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def get_all_factor_weights(*, db=None) -> list[dict]:
    """列出所有市场 × 因子的权重(含最近记录到的 IC/IR 观测),供只读 API/UI 展示。

    只读标定不落库,故 last_* 是「最后一次记录下来的观测」而非实时值;
    判断新鲜度请看 last_calibrated_at。
    """
    own = db is None
    db = db or SessionLocal()
    try:
        for m in MARKETS:
            get_factor_weights(m, db=db)  # 确保各市场已 seed
        rows = (
            db.query(FactorWeight)
            .order_by(FactorWeight.market, FactorWeight.factor_code)
            .all()
        )
        return [_serialize(r) for r in rows]
    finally:
        if own:
            db.close()


def set_factor_weight(
    factor_code: str, market: str, *,
    weight: float | None = None, is_pinned: bool | None = None,
    auto_calibrate: bool | None = None, db=None,
    actor: str | None = None, via: str = "api",
) -> dict:
    """手动覆盖某因子权重 / pin / 开关自动标定;权重变化写 manual 审计。

    actor 必须是**服务端已认证身份**(web 层从 `require_settings_write` 拿),
    与权重改动同一次 commit 落进历史行 meta, 事后可追溯是谁改的。
    绝不接受客户端自报的身份。
    """
    if factor_code not in CALIBRATABLE_FACTORS:
        raise ValueError(f"未知因子: {factor_code}")
    if market not in MARKETS:
        raise ValueError(f"未知市场: {market}")
    own = db is None
    db = db or SessionLocal()
    try:
        get_factor_weights(market, db=db)  # 确保行存在
        row = (
            db.query(FactorWeight)
            .filter(FactorWeight.factor_code == factor_code, FactorWeight.market == market)
            .first()
        )
        if weight is not None:
            old = float(row.weight)
            new = round(max(0.1, min(3.0, float(weight))), 4)  # 手动也有界,防误填
            if abs(new - old) >= 1e-9:
                row.weight = new
                row.reason = "manual"
                row.effective_from = utc_now()
                row.updated_at = utc_now()
                db.add(FactorWeightHistory(
                    factor_code=factor_code, market=market,
                    old_weight=old, new_weight=new, sample_size=0, reason="manual",
                    meta=({"by": actor} if actor else {}) | {"via": via},
                ))
        if is_pinned is not None:
            row.is_pinned = bool(is_pinned)
            row.updated_at = utc_now()
        if auto_calibrate is not None:
            row.auto_calibrate = bool(auto_calibrate)
            row.updated_at = utc_now()
        db.commit()
        return _serialize(row)
    finally:
        if own:
            db.close()
