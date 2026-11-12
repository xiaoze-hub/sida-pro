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
  (`require_settings_write`) 或由后台任务显式声明系统身份
  (`actor_kind="system"` + `system:` 前缀), 与改动同一次 commit 落进历史行 meta;
  **空身份直接拒绝**(历史上 `actor=None` 也能静默改全局权重且审计查不到人)。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from src.core.db_safe import safe_rollback
from src.core.timezone import utc_now
from src.db.session import SessionLocal
from src.db.models import AuditLog, FactorWeight, FactorWeightHistory

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

#: 谁在改权重。`user` 必须是服务端认证出来的自然人身份(web 层 `require_settings_write`);
#: `system` 只能由明确知道自己在做什么的后台任务显式声明, 且 actor 必须以
#: `system:` 前缀命名 —— 审计里一眼能区分「人改的」与「程序改的」。
ACTOR_KINDS = ("user", "system")
SYSTEM_ACTOR_PREFIX = "system:"

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
    actor: str | None = None, actor_id: str | None = None,
    actor_kind: str = "user",
    via: str = "api", ip: str = "",
) -> dict:
    """手动覆盖某因子权重 / pin / 开关自动标定;改动写审计。

    **身份是硬门槛(2026-11-12 复审)**: `actor` 必须非空, 且只接受两种来源 ——
    - `actor_kind="user"`(默认): web 层 `require_settings_write` 给出的服务端
      认证 username。绝不接受客户端自报的身份(请求体里的 actor/by 一律不看)。
    - `actor_kind="system"`: 后台任务显式声明的系统身份, 名字必须以 `system:`
      开头。历史上这类调用可以传 `actor=None` 静默改全局权重且审计里查不到人,
      现在空身份直接 ValueError, 不留「改了但无法追溯」的通道。

    审计落点分工(2026-11-12 独立审计):
    - **权重变化** → `FactorWeightHistory`(manual), 这是时间快照的来源,
      `get_factor_weights(as_of=...)` 按它回放历史; meta 带 by/by_kind/via。
    - **pin / auto_calibrate 翻转** → `AuditLog`, **不写** History ——
      开关不是权重快照, 写进 History 会让 as_of 回放多出一堆同权重行,
      污染「某时点生效权重」的语义。
    - 两类变化都各写一条 `AuditLog`(`action=update_factor_weight`),
      与变更同事务提交: 审计写不进去就整笔失败(自己开的 session 会回滚),
      不允许「改了但无据可查」。
    """
    who = (actor or "").strip()
    kind = (actor_kind or "").strip().lower()
    if kind not in ACTOR_KINDS:
        raise ValueError(
            f"未知 actor_kind: {actor_kind!r}(只接受 user / system)"
        )
    if not who:
        raise ValueError(
            "改因子权重必须带非空的可信操作者身份: 人工入口由 web 层 "
            "require_settings_write 传入 username, 系统身份须显式传 "
            "actor_kind='system' 且 actor 以 'system:' 开头"
        )
    if kind == "system" and not who.lower().startswith(SYSTEM_ACTOR_PREFIX):
        raise ValueError(
            "actor_kind='system' 时 actor 必须以 'system:' 开头, "
            "以便审计区分系统调用与自然人操作"
        )
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
        changes: dict[str, dict] = {}
        if weight is not None:
            old = float(row.weight)
            new = round(max(0.1, min(3.0, float(weight))), 4)  # 手动也有界,防误填
            if abs(new - old) >= 1e-9:
                changes["weight"] = {"before": round(old, 4), "after": round(new, 4)}
                row.weight = new
                row.reason = "manual" if kind == "user" else who  # 系统身份已带 system: 前缀
                row.effective_from = utc_now()
                row.updated_at = utc_now()
                db.add(FactorWeightHistory(
                    factor_code=factor_code, market=market,
                    old_weight=old, new_weight=new, sample_size=0, reason="manual",
                    meta={"by": who, "by_kind": kind, "via": via},
                ))
        if is_pinned is not None:
            before = bool(row.is_pinned)
            after = bool(is_pinned)
            if before != after:
                changes["is_pinned"] = {"before": before, "after": after}
                row.is_pinned = after
                row.updated_at = utc_now()
        if auto_calibrate is not None:
            before = bool(row.auto_calibrate)
            after = bool(auto_calibrate)
            if before != after:
                changes["auto_calibrate"] = {"before": before, "after": after}
                row.auto_calibrate = after
                row.updated_at = utc_now()
        if changes:
            db.add(
                AuditLog(
                    user_id=actor_id,
                    username=who,
                    action="update_factor_weight",
                    detail=json.dumps(
                        {
                            "factor_code": factor_code,
                            "market": market,
                            "actor_kind": kind,
                            "via": via,
                            "changes": changes,
                        },
                        ensure_ascii=False,
                    ),
                    ip=ip or "",
                )
            )
        db.commit()
        return _serialize(row)
    except Exception:
        # 自己开的 session: 审计/落库任一步失败就整笔回滚, 不留「改了但无审计」。
        # 调用方传入 db 时不动它的事务(那是调用方的边界), 由调用方决定回滚。
        if own:
            safe_rollback(db)
        raise
    finally:
        if own:
            db.close()
