"""因子自校准(M2):把孤立的 IC/IR 接进因子权重的轻量标定闭环。

把 `factor_eval.evaluate_factor_ic` 算出的每因子 IC/IR,符号感知地转成目标权重,
EMA 平滑 + clamp 后**提案**给人工;默认不改变任何生效权重。

设计要点(见 .docs/factor-self-calibration-design-2026-06-20.md):
- IC 必须测在「原始因子」上(快照存 raw,权重只在合成时乘),否则闭环自我强化失真。
- 惩罚因子(risk/crowd)IC 预期为负:用 −IC 驱动,惩罚有效→提权,失效→降权。
- 只吃「持有期已走完」的 outcome(point-in-time),防未来函数。

安全模式(2026-11-12):
- `mode="suggest"`(默认):只产出提案,不写 `FactorWeight.weight / reason /
  effective_from / meta`,不写 `FactorWeightHistory`,也不 lazy seed 权重行。
  自动化路径因此**不可能**悄悄改变有效权重。
- `mode="apply"`:保留原自动写权能力,但必须显式传 `approved_by`(人工授权),
  未授权直接 `ValueError` 拒绝,不做任何写入。
- 提案真正落地走 `apply_pending_proposal` → 复用既有
  `factor_weights.set_factor_weight`(manual 审计),不新增自动历史调整机制。
- 观测/提案持久化必须显式 `persist_proposals=True`,且只写**已存在**的权重行,
  绝不因为写 meta 而 seed 出新行。
"""

from __future__ import annotations

import logging

from src.core.factor_eval import evaluate_factor_ic
from src.core.factor_weights import (
    CALIBRATABLE_FACTORS,
    MARKETS,
    PENALTY_FACTORS,
    set_factor_weight,
)
from src.core.timezone import utc_now
from src.db.session import SessionLocal
from src.db.models import FactorWeight, FactorWeightHistory

logger = logging.getLogger(__name__)

# 归一化基准:一个「不错」的 IR / 一个「有意义」的单期 IC。
IR_REF = 0.5
IC_REF = 0.05

# B0.4(2026-09-09): 样本外门禁 —— 权重调整必须在 holdout 段仍成立。
# 取日期后 30% 作为 holdout; |holdout IC| 低于阈值或与主口径反号 → 本轮不调权。
OOS_RATIO = 0.3
OOS_MIN_IC = 0.02

# 提案/落地审计键(FactorWeight.meta 内)。**不**写 FactorWeightHistory ——
# 该历史表被 `get_factor_weights(as_of=...)` 当作真实权重快照回放,写提案进去会
# 污染时点复现。
PROPOSAL_META_KEY = "pending_proposal"
APPLIED_META_KEY = "applied_proposal"

MODE_SUGGEST = "suggest"
MODE_APPLY = "apply"
MODES = (MODE_SUGGEST, MODE_APPLY)

# 权重变化小于该阈值就不值得提/不写,避免抖动噪声。
MIN_DELTA = 0.01


def compute_target(factor_code: str, ic, ir, *, beta: float = 0.4) -> float | None:
    """由 IC/IR 算目标权重;优先 IR(更稳),fallback IC;惩罚因子翻符号。

    返回 None 表示信息不足(IC、IR 均缺失),应跳过该因子。
    term 归一化并 clamp 到 [-1, 1];target = 1 + beta·term。
    """
    if ir is not None:
        term = ir / IR_REF
    elif ic is not None:
        term = ic / IC_REF
    else:
        return None
    if factor_code in PENALTY_FACTORS:
        term = -term  # 惩罚因子:IC 越负越该信
    term = max(-1.0, min(1.0, term))
    return 1.0 * (1.0 + beta * term)


def blend(old: float, target: float, *, alpha: float = 0.35,
          lo: float = 0.5, hi: float = 1.5) -> float:
    """EMA 平滑(防跳变)+ clamp 到 [lo, hi]。"""
    new = old * (1.0 - alpha) + target * alpha
    return max(lo, min(hi, new))


def _validate_mode(mode: str, approved_by: str | None) -> str:
    """校验模式与人工授权;不合法直接抛 ValueError(绝不静默降级成写权重)。"""
    if mode not in MODES:
        raise ValueError(f"未知标定模式: {mode!r}(仅支持 {MODES})")
    if mode == MODE_APPLY and not (approved_by or "").strip():
        raise ValueError("apply 模式必须显式提供 approved_by(人工授权)")
    return (approved_by or "").strip()


def _read_rows(db, market: str) -> dict[str, FactorWeight]:
    """只读加载已有权重行;**不**做 lazy seed(避免观测副作用建行)。"""
    rows = db.query(FactorWeight).filter(FactorWeight.market == market).all()
    return {r.factor_code: r for r in rows}


def _observation(stats: dict) -> dict:
    """把 IC 统计提炼成观测快照(仅供展示/审计,不含权重决定)。"""
    return {
        "ic": stats.get("ic"),
        "ir": stats.get("ir"),
        "ic_holdout": stats.get("ic_holdout"),
        "ic_pooled": stats.get("ic_pooled"),
        "sample_size": int(stats.get("sample_size", 0) or 0),
    }


def _write_meta_observation(row: FactorWeight, obs: dict, *, proposal: dict | None) -> None:
    """显式审计:把观测(+可选待审提案)写进已存在行的 meta。

    不碰 weight / reason / effective_from;不写 History(历史表是时点权重快照)。
    没有新提案时清掉旧提案,避免陈旧提案被误落地。
    """
    meta = dict(row.meta or {})
    meta.update({
        "last_ic": obs["ic"],
        "last_ir": obs["ir"],
        "last_sample_size": obs["sample_size"],
        "last_ic_pooled": obs["ic_pooled"],
        "last_holdout_ic": obs["ic_holdout"],
        "last_calibrated_at": utc_now().isoformat(),
    })
    if proposal is not None:
        meta[PROPOSAL_META_KEY] = proposal
    else:
        meta.pop(PROPOSAL_META_KEY, None)
    row.meta = meta


def _build_proposal(code: str, market: str, current: float, stats: dict, *,
                    min_samples: int, beta: float, alpha: float,
                    oos_min_ic: float, lo: float, hi: float) -> dict:
    """对单因子算出提案(或跳过原因),纯计算,不碰 DB。"""
    obs = _observation(stats)
    ic, ir = obs["ic"], obs["ir"]
    holdout, n = obs["ic_holdout"], obs["sample_size"]
    out = {
        "factor_code": code,
        "market": market,
        "current_weight": round(float(current), 4),
        "base_weight": round(float(current), 4),
        "proposed_weight": None,
        "target": None,
        "status": "proposed",
        "ic": ic,
        "ir": ir,
        "ic_holdout": holdout,
        "sample_size": n,
    }
    if n < min_samples or ic is None:
        out["status"] = "skipped_low_sample"
        return out
    # 样本外门禁: 信息不足 / 强度不够 / 符号翻转 → 不出可执行提案
    if holdout is None or abs(float(holdout)) < float(oos_min_ic):
        out["status"] = "skipped_oos_insufficient"
        return out
    if float(ic) * float(holdout) < 0:
        out["status"] = "skipped_oos_sign_flip"
        return out
    target = compute_target(code, ic, ir, beta=beta)
    if target is None:
        out["status"] = "skipped_no_target"
        return out
    new = round(blend(float(current), target, alpha=alpha, lo=lo, hi=hi), 4)
    out["target"] = round(target, 4)
    out["proposed_weight"] = new
    if abs(new - float(current)) < MIN_DELTA:
        out["status"] = "unchanged"
    return out


def calibrate_factor_weights(
    market: str, *, mode: str = MODE_SUGGEST, approved_by: str | None = None,
    persist_proposals: bool = False,
    alpha: float = 0.35, beta: float = 0.4,
    clamp: tuple[float, float] = (0.5, 1.5),
    min_samples: int = 20, horizon: int = 5, days: int = 90,
    oos_ratio: float = OOS_RATIO, oos_min_ic: float = OOS_MIN_IC, db=None,
) -> dict:
    """对单个市场跑一轮因子标定,默认只产出提案(零写入)。

    mode="suggest"(默认):不建权重行、不写 meta、不写 History、不改
      weight/reason/effective_from;唯一产物是返回值里的 `proposals`。
    mode="apply":需显式 `approved_by`;按老口径写 FactorWeight + History
      (History.meta 记录 approved_by),即「人工授权后」的自动标定。
    persist_proposals=True:suggest 模式下把观测与待审提案写进**已存在**行的
      meta(供 UI/审计);不 seed 新行、不改权重、不写 History。

    门控一致:is_pinned / auto_calibrate=False / 样本不足 / IC 缺失 / OOS 不通过
    → 不出可执行提案(status 说明原因);OOS 拒绝只写提案 status,不写 meta。
    """
    approved = _validate_mode(mode, approved_by)  # 先校验,再做任何 DB 动作

    own = db is None
    db = db or SessionLocal()
    try:
        ic_result = evaluate_factor_ic(
            days=days, horizon=horizon, min_samples=min_samples, market=market,
            holdout_ratio=oos_ratio, db=db,
        )
        factors = ic_result.get("factors", {})

        # 只读加载,不预 seed:apply 模式也只在确实要落地的因子上补建行,
        # 免得失败回滚前就留下无意义的默认行。
        rows = _read_rows(db, market)

        lo, hi = float(clamp[0]), float(clamp[1])
        changed = 0
        applied = 0
        proposed = 0
        skipped_oos = 0
        skipped_low_sample = 0
        skipped_pinned = 0
        skipped_disabled = 0
        persisted = False
        rows_changed: list[dict] = []
        proposals: list[dict] = []

        for code in CALIBRATABLE_FACTORS:
            row = rows.get(code)
            current = float(row.weight) if row is not None else 1.0
            stats = factors.get(code, {}) or {}

            proposal = _build_proposal(
                code, market, current, stats, min_samples=min_samples,
                beta=beta, alpha=alpha, oos_min_ic=oos_min_ic, lo=lo, hi=hi,
            )
            if row is not None and row.is_pinned:
                proposal["status"] = "skipped_pinned"
            elif row is not None and not row.auto_calibrate:
                proposal["status"] = "skipped_disabled"

            status = proposal["status"]
            if status == "skipped_low_sample":
                skipped_low_sample += 1
            elif status in ("skipped_oos_insufficient", "skipped_oos_sign_flip"):
                skipped_oos += 1
            elif status == "skipped_pinned":
                skipped_pinned += 1
            elif status == "skipped_disabled":
                skipped_disabled += 1
            elif status == "proposed":
                proposed += 1
            proposals.append(proposal)

            if mode == MODE_SUGGEST:
                # 默认零写入;只有显式审计开关才写「已存在行」的 meta。
                if persist_proposals and row is not None:
                    _write_meta_observation(
                        row, _observation(stats),
                        proposal=proposal if status == "proposed" else None,
                    )
                    persisted = True
                continue

            # ---- mode == apply:显式人工授权后才写权重 ----
            if row is None and status == "proposed":
                row = FactorWeight(factor_code=code, market=market, weight=1.0)
                db.add(row)
                rows[code] = row
            if row is not None:
                _write_meta_observation(row, _observation(stats), proposal=None)
            if status != "proposed" or row is None:
                continue
            new = float(proposal["proposed_weight"])
            old = float(row.weight)
            if abs(new - old) < MIN_DELTA:
                continue

            row.weight = new
            row.reason = (f"auto(ic={proposal['ic']}, ir={proposal['ir']}, "
                          f"oos_ic={proposal['ic_holdout']}, n={proposal['sample_size']})")
            row.effective_from = utc_now()
            row.updated_at = utc_now()
            db.add(FactorWeightHistory(
                factor_code=code, market=market, old_weight=old, new_weight=new,
                ic=proposal["ic"], ir=proposal["ir"], sample_size=proposal["sample_size"],
                reason="auto",
                meta={
                    "target": proposal["target"], "alpha": alpha,
                    "oos_ic": proposal["ic_holdout"], "approved_by": approved,
                },
            ))
            changed += 1
            applied += 1
            rows_changed.append({
                "factor_code": code, "old_weight": old, "new_weight": new,
                "sample_size": proposal["sample_size"],
            })

        if mode == MODE_APPLY or persisted:
            db.commit()
        return {
            "market": market,
            "mode": mode,
            "approved_by": approved or None,
            "checked": len(CALIBRATABLE_FACTORS),
            "proposed": proposed,
            "changed": changed,
            "applied": applied,
            "skipped_oos": skipped_oos,
            "skipped_low_sample": skipped_low_sample,
            "skipped_pinned": skipped_pinned,
            "skipped_disabled": skipped_disabled,
            "persisted": persisted,
            "write_back": mode == MODE_APPLY,
            "rows": rows_changed,
            "proposals": proposals,
        }
    except Exception as e:  # pragma: no cover - 防御性
        logger.warning(f"[因子标定] market={market} mode={mode} 失败: {e}")
        db.rollback()
        return {
            "market": market, "mode": mode, "checked": 0, "proposed": 0,
            "changed": 0, "applied": 0, "skipped_oos": 0, "skipped_low_sample": 0,
            "skipped_pinned": 0, "skipped_disabled": 0, "persisted": False,
            "write_back": False, "rows": [], "proposals": [], "error": str(e),
        }
    finally:
        if own:
            db.close()


def calibrate_all_markets(*, db=None, mode: str = MODE_SUGGEST,
                          approved_by: str | None = None, **kwargs) -> dict[str, dict]:
    """对所有市场(CN/HK/US)各跑一轮因子标定;供调度器每日 outcome 评估后调用。

    默认 suggest:调度器只拿到提案,不改变任何有效权重。
    kwargs 透传给 calibrate_factor_weights(alpha/beta/clamp/min_samples/horizon/days)。
    """
    own = db is None
    db = db or SessionLocal()
    try:
        return {
            m: calibrate_factor_weights(m, db=db, mode=mode, approved_by=approved_by, **kwargs)
            for m in MARKETS
        }
    finally:
        if own:
            db.close()


def apply_pending_proposal(factor_code: str, market: str, *, approved_by: str, db=None) -> dict:
    """把某因子的待审提案落地:复用既有 manual 授权路径写权重 + manual 审计。

    前置:meta 里有 `pending_proposal`,且当前权重仍等于提案基线(否则说明期间
    有人改过权重,旧提案作废 → ValueError,要求重新生成)。
    落地后清掉 pending_proposal,并在 meta 留 `applied_proposal` 审计。
    """
    approved = (approved_by or "").strip()
    if not approved:
        raise ValueError("提案落地需要 approved_by(人工授权)")
    if factor_code not in CALIBRATABLE_FACTORS:
        raise ValueError(f"未知因子: {factor_code}")
    if market not in MARKETS:
        raise ValueError(f"未知市场: {market}")

    own = db is None
    db = db or SessionLocal()
    try:
        row = (
            db.query(FactorWeight)
            .filter(FactorWeight.factor_code == factor_code, FactorWeight.market == market)
            .first()
        )
        proposal = (row.meta or {}).get(PROPOSAL_META_KEY) if row is not None else None
        if row is None or not isinstance(proposal, dict) or proposal.get("proposed_weight") is None:
            raise ValueError(f"{factor_code}/{market} 无待审提案")
        if row.is_pinned:
            raise ValueError(f"{factor_code}/{market} 已 pin,不接受自动提案落地")
        base = proposal.get("base_weight", proposal.get("current_weight"))
        if base is None or abs(float(row.weight) - float(base)) >= 1e-9:
            raise ValueError(f"{factor_code}/{market} 提案基线已过期(当前权重已变化),请重新生成")

        new = round(float(proposal["proposed_weight"]), 4)
        # 既有手动授权路径:写权重 + manual 审计(clamp 0.1~3.0 语义不变)。
        set_factor_weight(factor_code, market, weight=new, db=db)

        row = (
            db.query(FactorWeight)
            .filter(FactorWeight.factor_code == factor_code, FactorWeight.market == market)
            .one()
        )
        meta = dict(row.meta or {})
        meta.pop(PROPOSAL_META_KEY, None)
        meta[APPLIED_META_KEY] = {
            "base_weight": float(base),
            "proposed_weight": new,
            "approved_by": approved,
            "applied_at": utc_now().isoformat(),
            "ic": proposal.get("ic"),
            "ir": proposal.get("ir"),
            "sample_size": proposal.get("sample_size"),
        }
        row.meta = meta
        row.updated_at = utc_now()
        db.commit()
        return {
            "factor_code": factor_code, "market": market, "weight": new,
            "old_weight": float(base), "approved_by": approved, "path": "manual",
        }
    except Exception:
        db.rollback()
        raise
    finally:
        if own:
            db.close()
