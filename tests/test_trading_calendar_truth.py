# -*- coding: utf-8 -*-
"""交易日历「真实行情真相」回归(2026-10-11 P1)。

钉子: 交易日历必须与**真实行情**一致, 而不是与另一张手抄表自比。

真相来源 = `tests/fixtures/real_trading_days.txt`(离线固化) —— 由生产 PG `klines`
(period='1d')全市场覆盖最高的 40 只 A 股标的 distinct 交易日并集导出(生成脚本
`scripts/gen_real_trading_days_fixture.py`, 只读)。该窗口(2025-01-02 ~ 2026-08-25)
无数据缺口, 每一天: calendar.is_trading_day(d) 必须等于 (d 在真清单内)。

覆盖的两类历史 bug:
  ① 调休补班周末(如 2025-10-11 周六 / 2026-10-10 周六)被误判为交易日 → A 股周末恒不开市;
  ② 2026-02-16(周一, 除夕)漏列 → 被误判为交易日(真实: 节前 02-13、节后 02-24)。
**离线**: 只读本地 fixture, 禁真网络。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from src.core import trading_calendar as tc

_FIXTURE = Path(__file__).parent / "fixtures" / "real_trading_days.txt"


def _load_truth() -> list[date]:
    if not _FIXTURE.exists():  # pragma: no cover
        pytest.fail(f"缺少真相 fixture: {_FIXTURE}")
    out: list[date] = []
    for line in _FIXTURE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(date.fromisoformat(line))
    return out


TRUTH = _load_truth()
TRUTH_SET = {d.isoformat() for d in TRUTH}
WINDOW_LO, WINDOW_HI = "2025-01-02", "2026-08-25"


def _window_dates():
    d = date.fromisoformat(WINDOW_LO)
    hi = date.fromisoformat(WINDOW_HI)
    while d <= hi:
        yield d
        d += timedelta(days=1)


def test_fixture_is_sane():
    """fixture 自身健全性: 非空、无周末、连续覆盖窗口、日期升序去重。"""
    assert len(TRUTH) >= 380, f"fixture 交易日过少({len(TRUTH)}), 可能被截断"
    assert TRUTH == sorted(TRUTH), "fixture 应升序"
    assert len(TRUTH) == len(TRUTH_SET), "fixture 有重复日期"
    weekend = [d for d in TRUTH if d.weekday() >= 5]
    assert weekend == [], f"A 股真交易日不应含周末: {weekend[:5]}"


def test_calendar_matches_real_truth_day_by_day():
    """窗口内逐日: calendar == 真实行情真相(单向也必须一致)。"""
    mismatches: list[str] = []
    for d in _window_dates():
        real = d.isoformat() in TRUTH_SET
        try:
            got = tc.is_trading_day(d)
        except Exception as e:  # noqa: BLE001
            got = f"ERR:{type(e).__name__}"
        if got != real:
            mismatches.append(f"{d.isoformat()}(dow{d.weekday()}) calendar={got} truth={real}")
    assert mismatches == [], (
        "交易日历与真实行情不一致(改表须以真实日线为准): " + "; ".join(mismatches[:40])
    )


@pytest.mark.parametrize(
    "d,expected,why",
    [
        # ① 调休补班周末: A 股不开市(真实行情核验)
        ("2025-01-26", False, "周日(春节调休) — 周末恒休市"),
        ("2025-02-08", False, "周六(春节调休)"),
        ("2025-04-27", False, "周日(劳动节调休)"),
        ("2025-09-28", False, "周日(国庆调休)"),
        ("2025-10-11", False, "周六(国庆调休)"),
        ("2026-02-14", False, "周六(春节调休)"),
        ("2026-02-28", False, "周六(春节调休)"),
        ("2026-04-26", False, "周日(劳动节调休)"),
        ("2026-05-09", False, "周六(劳动节调休)"),
        ("2026-10-10", False, "周六(国庆调休) — 真实行情无交易"),
        # ② 2026 除夕漏列(周一)
        ("2026-02-16", False, "周一·除夕 — 真实行情休市(节前 02-13 / 节后 02-24)"),
        # 正常交易日(锚点, 防止误伤)
        ("2026-02-13", True, "春节前最后交易日"),
        ("2026-02-24", True, "春节后首个交易日"),
        ("2025-10-09", True, "国庆后首个交易日"),
    ],
)
def test_specific_real_market_corrections(d, expected, why):
    assert tc.is_trading_day(d) is expected, f"{d} 应 {'交易' if expected else '休市'}({why})"


def test_2026_spring_festival_window_closed():
    """2026 春节: 02-16(除夕) ~ 02-23 全休, 02-24 开市。"""
    for d in ("2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
              "2026-02-21", "2026-02-22", "2026-02-23"):
        assert tc.is_trading_day(d) is False, f"{d} 春节应休市"
    assert tc.is_trading_day("2026-02-24") is True
