"""B6.4: 情绪因子 —— 关键词极性 + 时效衰减 + 日序列。"""

from __future__ import annotations

from src.core.sentiment import (
    recency_weight,
    score_text,
    sentiment_for_items,
    sentiment_series,
)


def test_score_text_polarity():
    s, p, n = score_text("公司中标重大项目并获增持")
    assert p >= 2 and n == 0 and s > 0
    s2, p2, n2 = score_text("公司被立案调查并遭减持")
    assert n2 >= 2 and p2 == 0 and s2 < 0
    assert score_text("") == (0.0, 0, 0)


def test_recency_weight_decays_and_floors():
    assert recency_weight(0) == 1.0
    assert abs(recency_weight(24) - 0.5) < 1e-9
    assert recency_weight(10_000) == 0.05


def test_sentiment_aggregation_and_confidence():
    items = [
        {"title": "公司中标大单", "age_hours": 0},
        {"title": "股东减持公告", "age_hours": 24},
        {"title": "日常经营公告", "age_hours": 2},   # 无命中
    ]
    res = sentiment_for_items(items)
    assert res.count == 2                      # 只统计命中条目
    assert res.positives == 1 and res.negatives == 1
    assert res.score > 0                       # 正向且未衰减, 负向衰减一半
    assert 0 < res.confidence < 1
    assert sentiment_for_items([]).confidence == 0.0


def test_sentiment_series_sorted():
    out = sentiment_series({
        "2026-09-02": [{"title": "预增"}],
        "2026-09-01": [{"title": "立案"}],
    })
    assert [x["date"] for x in out] == ["2026-09-01", "2026-09-02"]
    assert out[0]["score"] < 0 < out[1]["score"]
