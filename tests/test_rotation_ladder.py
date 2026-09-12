# -*- coding: utf-8 -*-
"""题材轮动 + 连板梯队(v0.5.81)纯函数测试。

轮动: 钉住"退榜题材必须留在行集合里"(老板: 题材是轮动的, 不能固定),
以及首日无前日基准时 new/exit 必须为空(不猜)。
梯队: 连板数从事件表自己推(不信任 limit_days 列), 沿**表内日期序列**数连续封板,
touch 未封不算; 空日 rows 为空不编造。
"""
from __future__ import annotations

from src.core.limit_ladder import boards_for_date, ladder_window
from src.core.theme_rotation import daily_top_sets, membership_flags, rotation_series

H = lambda d, c, s: {"trade_date": d, "block_code": c, "score": s}  # noqa: E731


def test_daily_top_sets_orders_and_cuts():
    hist = [H("2026-09-10", "A", 80), H("2026-09-10", "B", 90), H("2026-09-10", "C", None)]
    sets = daily_top_sets(hist, top_k=1)
    assert sets == {"2026-09-10": ["B"]}          # None 分不参与, 只取前 1


def test_rotation_new_and_exit():
    hist = [
        H("2026-09-09", "A", 80), H("2026-09-09", "B", 70),
        H("2026-09-10", "B", 85), H("2026-09-10", "C", 75),   # A 退, C 进
    ]
    sets = daily_top_sets(hist, top_k=2)
    seq = rotation_series(["2026-09-09", "2026-09-10"], sets)
    assert seq[0]["new_n"] == 0 and seq[0]["exit_n"] == 0      # 首日无基准不猜
    assert seq[1]["new_codes"] == ["C"] and seq[1]["exit_codes"] == ["A"]


def test_membership_flags_track_rotation():
    hist = [H("2026-09-09", "A", 80), H("2026-09-10", "B", 85), H("2026-09-11", "B", 60)]
    sets = daily_top_sets(hist, top_k=1)
    flags = membership_flags(["2026-09-09", "2026-09-10", "2026-09-11"], sets)
    assert flags["A"] == {"top_days": 1, "first_top_date": "2026-09-09",
                          "last_top_date": "2026-09-09", "in_top_today": False}
    assert flags["B"]["in_top_today"] is True and flags["B"]["top_days"] == 2


def _ev(d, sym, sealed=1):
    return {"trade_date": d, "symbol": sym, "is_sealed_close": sealed}


def test_boards_count_consecutive_sealed_days_via_table_dates():
    dates = ["2026-09-08", "2026-09-09", "2026-09-10"]     # 表内日期序列(跳过周末)
    events = [_ev("2026-09-08", "A"), _ev("2026-09-09", "A"), _ev("2026-09-10", "A"),
              _ev("2026-09-10", "B")]
    sealed = {"2026-09-08": {"A"}, "2026-09-09": {"A"}, "2026-09-10": {"A", "B"}}
    assert boards_for_date(dates, sealed, "2026-09-10") == {"A": 3, "B": 1}
    assert boards_for_date(dates, sealed, "nope") == {}


def test_touch_without_seal_does_not_count():
    events = [_ev("2026-09-09", "A", sealed=0), _ev("2026-09-10", "A", sealed=1)]
    lad = ladder_window(["2026-09-09", "2026-09-10"], events, names={"A": "甲"})
    assert lad[0]["rows"] == []                                # 只 touch 未封 → 空日不编造
    assert lad[1]["rows"] == [{"boards": 1, "codes": ["A"], "names": ["甲"], "sealed_n": 1}]


def test_ladder_groups_descending_and_empty_day():
    events = [_ev("2026-09-10", "A"), _ev("2026-09-09", "A"), _ev("2026-09-10", "B")]
    lad = ladder_window(["2026-09-09", "2026-09-10", "2026-09-11"], events)
    assert [r["boards"] for r in lad[1]["rows"]] == [2, 1]    # 降序
    assert lad[2]["rows"] == []                               # 空日不编造
