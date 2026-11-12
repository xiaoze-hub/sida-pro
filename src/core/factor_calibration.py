"""因子自校准(M2):把孤立的 IC/IR 接进因子权重的轻量标定闭环。

把 `factor_eval.evaluate_factor_ic` 算出的每因子 IC/IR,符号感知地转成目标权重,
EMA 平滑 + clamp 后**只产出建议**,绝不改变任何生效权重。

设计要点(见 .docs/factor-self-calibration-design-2026-06-20.md):
- IC 必须测在「原始因子」上(快照存 raw,权重只在合成时乘),否则闭环自我强化失真。
- 惩罚因子(risk/crowd)IC 预期为负:用 −IC 驱动,惩罚有效→提权,失效→降权。
- 只吃「持有期已走完」的 outcome(point-in-time),防未来函数。

只读约定(2026-11-12):
- 本模块**只读**:不建 `FactorWeight` 行、不写 `meta`、不写
  `FactorWeightHistory`、不改 `weight / reason / effective_from`。
  返回值里的 `proposals` 就是全部产物(含 IC/IR/holdout/样本量观测)。
- 不存在 `mode=apply` / `approved_by` / `persist_proposals` 之类的写权开关,
  也没有「提案落地」入口:历史上一度存在的自动写权重与提案持久化路径已撤除,
  自动化调度不可能悄悄改动有效权重。
- 改权重只能走既有手工 API(`factor_weights.set_factor_weight`,manual 审计);
  本模块不提供任何绕过人工授权的通道。
- 失败必须显式:IC 评估异常时返回 `error` 字段,不会把「失败」伪装成
  「本轮无提案」,也不会清空/覆盖任何已存观测(本模块不写观测)。
"""

from __future__ import annotations

import logging

from src.core.factor_eval import evaluate_factor_ic
from src.core.factor_weights import CALIBRATABLE_FACTORS, MARKETS, PENALTY_FACTORS
from src.db.session import SessionLocal
from src.db.models import FactorWeight

logger = logging.getLogger(__name__)

# 归一化基准:一个「不错」的 IR / 一个「有意义」的单期 IC。
IR_REF = 0.5
IC_REF = 0.05

# B0.4(2026-09-09): 样本外门禁 —— 提案必须在 holdout 段仍成立。
# 取日期后 30% 作为 holdout; |holdout IC| 低于阈值或与主口径反号 → 不出可执行提案。
OOS_RATIO = 0.3
OOS_MIN_IC = 0.02

# 权重变化小于该阈值就不值得提,避免抖动噪声。
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


def _read_rows(db, market: str) -> dict[str, FactorWeight]:
    """只读加载已有权重行;**不**做 lazy seed(避免读取副作用建行)。"""
    rows = db.query(FactorWeight).filter(FactorWeight.market == market).all()
    return {r.factor_code: r for r in rows}


def _observation(stats: dict) -> dict:
    """把 IC 统计提炼成观测快照(仅供展示/日志,不含权重决定)。"""
    return {
        "ic": stats.get("ic"),
        "ir": stats.get("ir"),
        "ic_holdout": stats.get("ic_holdout"),
        "ic_pooled": stats.get("ic_pooled"),
        "sample_size": int(stats.get("sample_size", 0) or 0),
    }


def _build_proposal(code: str, market: str, current: float, stats: dict, *,
                    min_samples: int, beta: float, alpha: float,
                    oos_min_ic: float, lo: float, hi: float) -> dict:
    """对单因子算出建议(或跳过原因),纯计算,不碰 DB。"""
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
    # 样本外门禁: 信息不足 / 强度不够 / 符号翻转 → 不出可执行建议
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
    market: str, *,
    alpha: float = 0.35, beta: float = 0.4,
    clamp: tuple[float, float] = (0.5, 1.5),
    min_samples: int = 20, horizon: int = 5, days: int = 90,
    oos_ratio: float = OOS_RATIO, oos_min_ic: float = OOS_MIN_IC, db=None,
) -> dict:
    """对单个市场跑一轮因子标定,**只产出建议**(零写入)。

    不建权重行、不写 meta、不写 History、不改 weight/reason/effective_from;
    唯一产物是返回值里的 `proposals`(每因子含 ic/ir/holdout/样本量观测)。

    门控一致:is_pinned / auto_calibrate=False / 样本不足 / IC 缺失 /
    OOS 不通过 → 不出可执行建议(status 说明原因),同样零写入。

    失败显式:评估异常 → 返回 `error`,不静默伪装成「无提案」。
    """
    own = db is None
    db = db or SessionLocal()
    try:
        ic_result = evaluate_factor_ic(
            days=days, horizon=horizon, min_samples=min_samples, market=market,
            holdout_ratio=oos_ratio, db=db,
        )
        factors = ic_result.get("factors", {})

        # 只读加载,不预 seed:避免观测副作用建行。
        rows = _read_rows(db, market)

        lo, hi = float(clamp[0]), float(clamp[1])
        proposed = 0
        skipped_oos = 0
        skipped_low_sample = 0
        skipped_pinned = 0
        skipped_disabled = 0
        proposals: list[dict] = []

        for code in CALIBRATABLE_FACTORS:
            row = rows.get(code)
            current = float(row.weight) if row is not None else 1.0
            stats = factors.get(code, {}) or {}

            proposal = _build_proposal(
                code, market, current, stats, min_samples=min_samples,
                beta=beta, alpha=alpha, oos_min_ic=oos_min_ic, lo=lo, hi=hi,
            )
            # pin / 关闭自动标定的因子即使有强 IC 也只给「被跳过」的建议。
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

        return {
            "market": market,
            "mode": "readonly",
            "checked": len(CALIBRATABLE_FACTORS),
            "proposed": proposed,
            # 只读模式下恒为 0 / False:保留键位,调用方不会把「建议」误读成「已生效」。
            "changed": 0,
            "write_back": False,
            "rows": [],
            "skipped_oos": skipped_oos,
            "skipped_low_sample": skipped_low_sample,
            "skipped_pinned": skipped_pinned,
            "skipped_disabled": skipped_disabled,
            "proposals": proposals,
        }
    except Exception as e:  # pragma: no cover - 防御性
        logger.warning(
            "[因子标定] market=%s 只读计算失败(未写入任何数据): %s", market, e
        )
        db.rollback()
        return {
            "market": market, "mode": "readonly", "checked": 0, "proposed": 0,
            "changed": 0, "write_back": False, "rows": [],
            "skipped_oos": 0, "skipped_low_sample": 0, "skipped_pinned": 0,
            "skipped_disabled": 0, "proposals": [], "error": str(e),
        }
    finally:
        if own:
            db.close()


def calibrate_all_markets(*, db=None, **kwargs) -> dict[str, dict]:
    """对所有市场(CN/HK/US)各跑一轮只读标定;供调度器 outcome 评估后调用。

    只回建议,不改变任何有效权重;单个市场失败只影响该市场的 `error` 字段。
    kwargs 透传给 calibrate_factor_weights(alpha/beta/clamp/min_samples/horizon/days)。
    """
    own = db is None
    db = db or SessionLocal()
    try:
        return {
            m: calibrate_factor_weights(m, db=db, **kwargs)
            for m in MARKETS
        }
    finally:
        if own:
            db.close()
