"""全量路径指纹去重单测 (2026-09-04: 主力买卖 10.54亿 vs 成交 6.05亿熔断事故)。

并发翻页页内容漂移 → 同一笔成交被拉到两次 → 全量路径此前无去重。
纯函数断言, 不联网。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages/marketdata/src"))

from src.core.dark_flow import _dedup_ticks


def _t(t, price, amt, seq):
    return {"t": t, "price": price, "amt": amt, "vol": 100, "d": "B", "_seq": seq}


def test_dedup_removes_exact_duplicates():
    ticks = [_t("09:30:03", 10.55, 2161690.0, 2), _t("09:30:03", 10.55, 2161690.0, 102)]
    out = _dedup_ticks(ticks)
    assert len(out) == 1
    assert sum(x["amt"] for x in out) == 2161690.0


def test_dedup_keeps_distinct_ticks():
    ticks = [_t("09:30:03", 10.55, 2161690.0, 2), _t("09:30:06", 10.57, 1982316.0, 3)]
    assert len(_dedup_ticks(ticks)) == 2


def test_dedup_sorted_by_time():
    ticks = [_t("09:30:06", 10.57, 1.0, 3), _t("09:30:03", 10.55, 2.0, 2)]
    out = _dedup_ticks(ticks)
    assert [x["t"] for x in out] == ["09:30:03", "09:30:06"]


def test_dedup_empty():
    assert _dedup_ticks([]) == []


def test_dedup_total_conservation():
    # 模拟翻页重叠: 同一批 3 笔被拉两遍 → 去重后总额不变
    batch = [_t("09:30:03", 10.55, 2161690.0, 2), _t("09:30:06", 10.57, 1982316.0, 3),
             _t("09:30:09", 10.59, 1938549.0, 4)]
    out = _dedup_ticks(batch + [dict(x, _seq=x["_seq"] + 100) for x in batch])
    assert len(out) == 3
    assert abs(sum(x["amt"] for x in out) - 6082555.0) < 0.01
