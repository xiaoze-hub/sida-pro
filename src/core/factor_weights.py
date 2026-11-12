"""因子权重存取层(M1)。

把信号合成时各因子的权重从「隐式 = 1 的硬编码」变成「外置 + 可标定」:
- `get_factor_weights(market)`:读取某市场各因子权重;缺失按 1.0 返回(纯读,
  **不建行**);供 `strategy_engine._compute_factor_breakdown` 合成 raw_score 时按因子相乘。
- `get_all_factor_weights()`:权重面板用的全量列表, 会补齐缺失行(写路径)。
- `set_factor_weight()`:唯一的手工改权入口。

标定逻辑见 `factor_calibration.py`;只读/手动覆盖的 API 在 `web/api/factors.py`。

读写分离口径(2026-11-12 复审):
- `get_factor_weights` 是**纯读**, 不再 lazy seed —— 读路径可能在只读副本上执行,
  在那里 INSERT 会直接报「readonly database」;缺行语义就是默认权重 1.0。
- 会写的地方(`get_all_factor_weights` 补行、`set_factor_weight` 改权重)统一
  经 `pin_write_bind`: 事务从第一条语句起就绑在**写库**, 旧值也取自写库,
  不会拿副本的滞后值当 before。

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
import threading
from datetime import datetime, timedelta, timezone

from src.core.db_safe import safe_rollback
from src.core.timezone import utc_now
from src.db.session import SessionLocal, WriteSessionLocal, pin_write_bind
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

#: 进程内写序列化(**不是全局互斥**): 权重写入是「读旧值 → 写新值 → 记历史」,
#: 两笔并发若都读到同一个旧值, 历史链就断成 `1.0→1.2` + `1.0→1.8`, 后一笔的
#: before 是错的, `get_factor_weights(as_of=...)` 回放会把中间那次改动吞掉。
#: 这里用可重入锁把同一进程的读改写串起来; 跨进程的正确性靠下面
#: `_load_row_for_update` 的行锁(PG/MySQL), SQLite 且多进程时**没有**跨进程
#: 保证 —— 这一点写在函数注释里, 不假装是全局锁。
_WRITE_LOCK = threading.RLock()


def _parse_observed_at(raw) -> datetime | None:
    """把 meta 里的 ISO 时间串解析成带时区 datetime; 解析不了就当没有。"""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _rows_for_market(db, market: str) -> dict[str, float]:
    rows = db.query(FactorWeight).filter(FactorWeight.market == market).all()
    return {r.factor_code: float(r.weight) for r in rows}


def _ensure_rows(db, market: str) -> dict[str, float]:
    """在**写库会话**里补齐该市场缺失的因子行(不 commit, 由调用方定事务边界)。

    只有写路径(权重面板列表 / 手工改权)调用它: 读路径不要建行, 否则在只读
    副本上执行时会直接失败。
    """
    existing = _rows_for_market(db, market)
    missing = [f for f in CALIBRATABLE_FACTORS if f not in existing]
    for f in missing:
        db.add(FactorWeight(factor_code=f, market=market, weight=1.0))
        existing[f] = 1.0
    if missing:
        db.flush()
    return existing


def _load_row_for_update(db, factor_code: str, market: str):
    """在写事务里读权重行并加行锁(SQLite 忽略 `FOR UPDATE`, 由 `_WRITE_LOCK` 兜)。

    `FOR UPDATE` 让并发事务在 PG/MySQL 上串行读到**对方已提交的最新值**,
    而不是各自缓存一份旧 `weight` 后再写历史 —— 后者会让历史链的 before 失真。
    """
    return (
        db.query(FactorWeight)
        .filter(
            FactorWeight.factor_code == factor_code,
            FactorWeight.market == market,
        )
        .with_for_update()
        .first()
    )


def get_factor_weights(market: str, *, as_of=None, db=None) -> dict[str, float]:
    """读取某市场各可标定因子的权重;缺失因子按 1.0 返回。

    **纯读**: 不建行、不 commit —— 本函数可能运行在读副本上;想要「补齐默认行」
    请用 `get_all_factor_weights`(它显式走写库)。

    as_of(B0.5, 2026-09-09): 传入 datetime 时按 `FactorWeightHistory` 取该时点**之前**
    最近一次生效的权重(历史快照可复现, 不套用今天的权重); 无历史记录则回落当前值。
    返回 {factor_code: weight},键恒为 CALIBRATABLE_FACTORS 全集。
    """
    own = db is None
    db = db or SessionLocal()  # 纯读: 允许路由到读副本
    try:
        existing = _rows_for_market(db, market)
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

    面板首次打开时库里可能还没有权重行, 这里会**补齐默认行** —— 因为补行是写操作,
    整个函数钉在写库(`pin_write_bind`), 不会在读副本上尝试 INSERT。
    注意: 补行会 commit 传入会话的当前事务(调用方应传专用写会话, 别拿它夹带
    自己的未提交改动)。

    只读标定不落库,故 last_* 是「最后一次记录下来的观测」而非实时值;
    判断新鲜度请看 last_calibrated_at。
    """
    own = db is None
    db = db or WriteSessionLocal()
    pin_write_bind(db)
    try:
        # 补行也是写: 与读改写共用同一把进程内锁, 避免两个请求同时 INSERT 同一
        # (factor_code, market) 撞唯一键。
        with _WRITE_LOCK:
            for m in MARKETS:
                _ensure_rows(db, m)
            db.commit()
        rows = (
            db.query(FactorWeight)
            .order_by(FactorWeight.market, FactorWeight.factor_code)
            .all()
        )
        return [_serialize(r) for r in rows]
    except Exception:
        # 只回滚自己开的事务; 调用方传入的 session 由调用方决定事务边界。
        if own:
            safe_rollback(db)
        raise
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

    读写分离(2026-11-12 复审): 本函数**先读旧值再写新值**, 且读路径必须在写库上,
    否则 before/after 会用副本的滞后值, 甚至把 UPDATE 发到只读副本。所以:
    - 自开 session 用 `WriteSessionLocal()`(专用写引擎);
    - 调用方传入会话时先 `pin_write_bind`(已在副本事务里 ⇒ 直接报错, 不静默分裂);
    - 补行 / 改权重 / 历史 / 审计在**同一个事务**里 commit。

    并发写(2026-11-12 复审二): 「读旧值 → 写新值 → 记历史」不是原子语句。两笔
    并发若都读到 1.0, 历史会出现两行 before=1.0(后一笔的 before 是错的),
    `as_of` 回放会把中间改动吞掉。因此: 进程内 `_WRITE_LOCK` 串行化 + 读行时
    `FOR UPDATE`(PG/MySQL 行锁)双保险。SQLite 忽略 `FOR UPDATE`, 多进程部署在
    SQLite 上**没有**跨进程保证 —— 生产用 PG 时行锁生效; 这里不宣称全局互斥。
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
    db = db or WriteSessionLocal()
    pin_write_bind(db)
    try:
        with _WRITE_LOCK:
            _ensure_rows(db, market)  # 补行也落在同一个写事务里
            row = _load_row_for_update(db, factor_code, market)
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
