"""情绪因子(B6.4): 新闻/公告文本 → 可聚合的情绪分。

v1 口径(可调、可解释, 不做黑箱):
- 关键词极性: 正向词 +1 / 负向词 −1.2(负面冲击通常更剧烈), 命中即累加;
- 时效衰减: 与 `_load_news_metrics` 同口径, 48h 线性衰减(下限 0.05);
- 输出 `score`(加权和) / `confidence`(命中条数越多越高, 0-1) / `count`。

纯函数、无网络、无 DB, 便于单测与在研究脚本中复用。
"""

from __future__ import annotations

from dataclasses import dataclass

POSITIVE_KEYWORDS: tuple[str, ...] = (
    "中标", "签约", "增持", "回购", "预增", "扭亏", "涨停", "突破", "获批",
    "合作", "订单", "提价", "分红", "新高", "业绩超预期", "国产替代",
)

NEGATIVE_KEYWORDS: tuple[str, ...] = (
    "减持", "质押", "预亏", "亏损", "问询", "处罚", "立案", "退市", "跌停",
    "商誉减值", "解禁", "违规", "诉讼", "停产", "业绩不及预期", "爆雷",
)

_NEG_WEIGHT = 1.2


@dataclass(frozen=True)
class SentimentResult:
    score: float
    confidence: float
    count: int
    positives: int
    negatives: int


def score_text(text: str) -> tuple[float, int, int]:
    """单条文本 → (极性分, 正向命中数, 负向命中数)。"""
    t = (text or "").lower()
    if not t:
        return 0.0, 0, 0
    pos = sum(1 for kw in POSITIVE_KEYWORDS if kw in t)
    neg = sum(1 for kw in NEGATIVE_KEYWORDS if kw in t)
    return pos - neg * _NEG_WEIGHT, pos, neg


def recency_weight(age_hours: float, *, half_life_hours: float = 48.0) -> float:
    """时效权重: 48h 线性衰减, 下限 0.05。"""
    if half_life_hours <= 0:
        return 1.0
    return max(0.05, 1.0 - max(0.0, float(age_hours)) / half_life_hours)


def sentiment_for_items(items: list[dict]) -> SentimentResult:
    """聚合一组新闻: items = [{text|title|content, age_hours?}]。

    score = Σ 极性分 × 时效权重; confidence = 1 − 1/(1+命中条数)。
    """
    total = 0.0
    pos = neg = 0
    hit = 0
    for it in items or []:
        text = str(it.get("text") or it.get("title") or it.get("content") or "")
        s, p, n = score_text(text)
        if p == 0 and n == 0:
            continue
        w = recency_weight(float(it.get("age_hours") or 0.0))
        total += s * w
        pos += p
        neg += n
        hit += 1
    confidence = round(1.0 - 1.0 / (1.0 + hit), 4) if hit else 0.0
    return SentimentResult(
        score=round(total, 4), confidence=confidence, count=hit,
        positives=pos, negatives=neg,
    )


def sentiment_series(items_by_date: dict[str, list[dict]]) -> list[dict]:
    """按日聚合情绪序列(升序), 供前端画情绪曲线/与价格对照。"""
    out: list[dict] = []
    for d in sorted(items_by_date):
        res = sentiment_for_items(items_by_date[d])
        out.append({
            "date": d,
            "score": res.score,
            "confidence": res.confidence,
            "count": res.count,
        })
    return out
