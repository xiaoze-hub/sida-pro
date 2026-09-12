"""B6.8: 数智决策 1/3/5 日序列 + 0 轴穿越。"""

from __future__ import annotations

from src.core.decision_pioneer import compute_gs_signal, compute_gs_windows


def _bars(closes: list[float]) -> list[dict]:
    return [
        {"date": f"2026-08-{i + 1:02d}", "open": c, "high": c * 1.01,
         "low": c * 0.99, "close": c, "volume": 1000}
        for i, c in enumerate(closes)
    ]


def _crossing_closes() -> list[float]:
    """先下跌(空头)后上涨 → A0 上穿 BB0(0 轴穿越)。"""
    down = [10.0 - i * 0.05 for i in range(30)]
    up = [down[-1] + i * 0.12 for i in range(1, 20)]
    return down + up


def test_gs_windows_reports_crossing_and_series():
    bars = _bars(_crossing_closes())
    out = compute_gs_windows(bars, windows=(1, 3, 5, 50))
    assert out is not None
    assert out["state"] in ("G区", "S区")
    assert set(out["windows"].keys()) == {"1d", "3d", "5d", "50d"}
    for w in ("1d", "3d", "5d", "50d"):
        seg = out["windows"][w]
        assert set(seg.keys()) == {"crossings", "cum_dif", "last_dir"}
        assert seg["crossings"] >= 0
    # 上穿发生在近期但未必落在最近 5 根内 → 用全窗口断言捕捉到穿越
    assert out["windows"]["50d"]["crossings"] >= 1
    assert out["last_cross"]["direction"] == "G"
    assert isinstance(out["last_cross"]["bars_ago"], int)


def test_gs_windows_consistent_with_gs_signal():
    bars = _bars(_crossing_closes())
    sig = compute_gs_signal(bars)
    win = compute_gs_windows(bars)
    assert sig is not None and win is not None
    assert win["state"] == sig["state"]
    assert abs(win["dif"] - (sig["a0"] - sig["bb0"])) < 1e-6


def test_gs_windows_fail_soft():
    assert compute_gs_windows([]) is None
    assert compute_gs_windows(_bars([10.0, 10.1, 10.2])) is None
