"""B7 钉子: 内部计量的**耗时口径** —— 未记录 ≠ 0ms, 空样本不给分位数。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.core.usage_latency import aggregate_latency, percentile


@dataclass
class Row:
    """与 `skill_usage` ORM 行同形(便于不碰数据库地测聚合)。"""

    skill_name: str
    status_code: int
    duration_ms: int
    created_at: datetime | None
    user_id: str | None = None


def _at(day: str, h: int = 10, m: int = 0) -> datetime:
    return datetime.fromisoformat(f"{day}T{h:02d}:{m:02d}:00")


def test_percentile_is_nearest_rank_and_empty_is_none():
    """分位数用最近秩; **空样本返回 None, 不是 0**。"""
    assert percentile([], 0.5) is None
    assert percentile([10, 20, 30, 40], 0.5) == 20       # 偶数样本取较小一侧
    assert percentile(list(range(1, 101)), 0.95) == 95
    assert percentile([7], 0.95) == 7


def test_zero_duration_is_not_counted_as_fast():
    """**duration_ms=0 是"未记录", 不许当成"很快"混进分位数**。"""
    rows = [
        Row("kline", 200, 0, _at("2026-09-19")),      # 未记录
        Row("kline", 200, 900, _at("2026-09-19")),
        Row("kline", 200, 1100, _at("2026-09-19")),
    ]
    out = aggregate_latency(rows, days=7, today="2026-09-19")
    assert out["totals"]["calls"] == 3
    assert out["totals"]["p50_ms"] == 900            # 0 那条约等于被排除
    assert "1 次未记录耗时" in out["latency_note"]
    assert out["by_skill"][0]["latency_samples"] == 2


def test_all_unrecorded_gives_no_percentile():
    """全是未记录时: 分位数必须是 None + 说清原因, 不许给 0。"""
    rows = [Row("darkflow", 200, 0, _at("2026-09-19")) for _ in range(5)]
    out = aggregate_latency(rows, days=7, today="2026-09-19")
    assert out["totals"]["p50_ms"] is None
    assert out["totals"]["p95_ms"] is None
    assert out["by_skill"][0]["p50_ms"] is None
    assert "5 次未记录耗时" in out["latency_note"]


def test_errors_reported_alongside_latency():
    """错误数与调用数一起给 —— 只看 p95 会漏掉错误率。"""
    rows = [
        Row("kline", 200, 100, _at("2026-09-19")),
        Row("kline", 500, 300, _at("2026-09-19")),
    ]
    out = aggregate_latency(rows, days=7, today="2026-09-19")
    assert out["totals"]["errors"] == 1
    assert out["by_skill"][0]["errors"] == 1


def test_grouped_by_day_and_skill():
    rows = [
        Row("kline", 200, 100, _at("2026-09-18")),
        Row("kline", 200, 200, _at("2026-09-18", 11)),
        Row("darkflow", 200, 800, _at("2026-09-19")),
    ]
    out = aggregate_latency(rows, days=7, today="2026-09-19")
    days = {d["day"]: d for d in out["by_day"]}
    assert set(days) == {"2026-09-18", "2026-09-19"}
    assert days["2026-09-18"]["calls"] == 2
    assert days["2026-09-18"]["p50_ms"] == 100
    skills = {s["skill"]: s for s in out["by_skill"]}
    assert skills["kline"]["calls"] == 2 and skills["darkflow"]["calls"] == 1
    assert skills["kline"]["p50_ms"] == 100


def test_users_counted_per_skill_and_missing_user_not_dropped():
    """按 skill 数**去重用户数**; user_id 为空的行仍计入调用数(不静默丢弃)。"""
    rows = [
        Row("kline", 200, 100, _at("2026-09-19"), user_id="u1"),
        Row("kline", 200, 120, _at("2026-09-19"), user_id="u1"),
        Row("kline", 200, 130, _at("2026-09-19"), user_id="u2"),
        Row("kline", 200, 140, _at("2026-09-19"), user_id=None),
    ]
    out = aggregate_latency(rows, days=7, today="2026-09-19")
    k = out["by_skill"][0]
    assert k["calls"] == 4           # 无归属的那条也在
    assert k["users"] == 2           # 去重后的真实用户数


def test_unknown_date_not_silently_merged():
    """created_at 缺失 → 归到"未知日期"一行, 不假装是今天。"""
    rows = [Row("kline", 200, 100, None)]
    out = aggregate_latency(rows, days=7, today="2026-09-19")
    assert out["by_day"][0]["day"] == "未知日期"
