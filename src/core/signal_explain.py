"""信号可解释化(Phase 3):rank_score → 1-10 AI Score + 正负因子拆解。

对标 Danelfin 的 1-10 AI Score 与 green/red AI Factors:把 strategy_engine 已算出的
score_breakdown(alpha/catalyst/quality/source_bonus 加分项,risk/crowd penalty 扣分项)
拆成「正向(绿,提升)/ 负向(红,拖累)」两组,供机会页展示。

纯函数,不依赖 DB;在 API 层对 list_strategy_signals 的结果做后处理注入。
"""

from __future__ import annotations

# 因子中文标签
FACTOR_LABELS = {
    "alpha_score": "选股α",
    "catalyst_score": "催化",
    "quality_score": "计划质量",
    "source_bonus": "来源加成",
    "risk_penalty": "风险",
    "crowd_penalty": "拥挤度",
}

# 加分类因子(正值=提升,负值=拖累)
ADDITIVE_FACTORS = ("alpha_score", "catalyst_score", "quality_score", "source_bonus")
# 惩罚类因子(正值=拖累,score_breakdown 中以正数表示惩罚强度)
PENALTY_FACTORS = ("risk_penalty", "crowd_penalty")

_EPS = 0.01


def to_ai_score(rank_score) -> int:
    """rank_score(0-100)→ 1-10 AI Score(clamp 到 [1,10])。"""
    try:
        s = float(rank_score or 0.0)
    except (TypeError, ValueError):
        s = 0.0
    return max(1, min(10, round(s / 10.0)))


def explain_factors(score_breakdown) -> dict:
    """拆成正向(绿)/负向(红)两组,各按贡献绝对值排序取前 5。"""
    sb = score_breakdown if isinstance(score_breakdown, dict) else {}
    positive: list[dict] = []
    negative: list[dict] = []

    def _f(key):
        try:
            return float(sb.get(key))
        except (TypeError, ValueError):
            return None

    for key in ADDITIVE_FACTORS:
        v = _f(key)
        if v is None:
            continue
        if v > _EPS:
            positive.append({"factor": key, "label": FACTOR_LABELS.get(key, key), "contribution": round(v, 2)})
        elif v < -_EPS:
            negative.append({"factor": key, "label": FACTOR_LABELS.get(key, key), "contribution": round(v, 2)})

    for key in PENALTY_FACTORS:
        v = _f(key)
        if v is None:
            continue
        if v > _EPS:  # 惩罚为正 = 拖累,贡献记为负
            negative.append({"factor": key, "label": FACTOR_LABELS.get(key, key), "contribution": round(-v, 2)})

    positive.sort(key=lambda x: x["contribution"], reverse=True)
    negative.sort(key=lambda x: x["contribution"])  # 最负在前
    return {"positive": positive[:5], "negative": negative[:5]}


def build_signal_evidence(item: dict) -> dict:
    """GS/策略信号的证据链(触发条件 + 数据时点 + 失效条件)。纯函数, 无 IO。

    - 触发条件: 正向因子(哪些因子在加分) + 评分;
    - as_of: item 的 snapshot_date / trade_date / as_of(缺失显式「时点缺失」);
    - 失效条件: item 自带 `invalidation`(若后端给了); 缺失则由**负向因子**拼出
      「若这些拖累因子解除/反转则结论改变」+ 通用兜底, 绝不空。
    """
    from src.core.evidence_chain import build_evidence_chain

    it = item if isinstance(item, dict) else {}
    fe = it.get("factor_explain")
    if not isinstance(fe, dict):
        fe = explain_factors(it.get("score_breakdown"))
    pos = [str(p.get("label") or p.get("factor") or "") for p in fe.get("positive", [])]
    pos = [p for p in pos if p]
    neg = [str(p.get("label") or p.get("factor") or "") for p in fe.get("negative", [])]
    neg = [n for n in neg if n]

    trig = [f"正向因子: {p}" for p in pos]
    score = it.get("rank_score")
    if score is not None:
        trig.append(f"综合评分 {score}")
    if not trig:
        trig = ["无正向因子(评分=基线)"]

    inval = it.get("invalidation")
    if not inval and neg:
        inval = [f"若拖累因子(「{'、'.join(neg)}」)解除或反转, 则此结论改变"]
    default_inval = ["若正向因子转弱 或 风险/拥挤度恶化, 则此评分作废"]

    as_of = it.get("snapshot_date") or it.get("trade_date") or it.get("as_of")
    return build_evidence_chain(
        triggers=trig,
        as_of=as_of,
        invalidation=inval,
        default_invalidation=default_inval,
    )


def enrich_signal(item: dict) -> dict:
    """给一条信号 item 注入 ai_score + factor_explain + evidence(证据链)(原地修改并返回)。"""
    if not isinstance(item, dict):
        return item
    item["ai_score"] = to_ai_score(item.get("rank_score"))
    item["factor_explain"] = explain_factors(item.get("score_breakdown"))
    item["evidence_chain"] = build_signal_evidence(item)
    return item
